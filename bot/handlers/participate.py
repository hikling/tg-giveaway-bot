from __future__ import annotations

import logging
import math
from datetime import UTC, datetime

from aiogram import Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.callbacks.factories import JoinCb, PoetryCb
from bot.db.base import get_sessionmaker
from bot.db.models import GiveawayStatus
from bot.db.repo import claim_giveaway, ensure_user, get_giveaway, has_participated, is_blacklisted
from bot.i18n import t
from bot.poetry import POEMS
from bot.services.channels import display_requirement, linked_requirements, missing_channels
from bot.services.giveaway import draw_if_full, publish_draw
from bot.services.notifier import winner_delivery_lock
from bot.services.poetry import Challenge, answer, get_challenge, record_delivery
from bot.settings import get_settings
from bot.utils.antiabuse import account_age_gate

log = logging.getLogger(__name__)


def question_message(challenge: Challenge) -> tuple[str, InlineKeyboardMarkup]:
    poem = POEMS[challenge.question_id]
    text = t(
        "poetry.question",
        id=challenge.giveaway_id,
        title=poem.title,
        author=poem.author,
        prompt=poem.prompt,
        remaining=challenge.remaining,
        seconds=max(0, math.ceil((challenge.expires_at - datetime.now(UTC)).total_seconds()))
        if challenge.expires_at
        else 60,
    )
    buttons = [
        InlineKeyboardButton(
            text=f"{i + 1}．{option}",
            callback_data=PoetryCb(
                giveaway_id=challenge.giveaway_id, nonce=challenge.nonce, choice=i
            ).pack(),
        )
        for i, option in enumerate(challenge.options)
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:]])


async def _eligible(cq: CallbackQuery, gid: int) -> bool:
    settings = get_settings()
    uid = cq.from_user.id
    if settings.account_age_gate and not account_age_gate(uid, settings.min_user_id):
        await cq.answer(t("join.too_new"), show_alert=True)
        return False
    async with get_sessionmaker()() as session:
        gw = await get_giveaway(session, gid)
        if (
            gw is None
            or gw.status != GiveawayStatus.running
            or (gw.draw_mode != "count" and gw.end_at <= datetime.now(UTC))
        ):
            await cq.answer(t("join.ended"), show_alert=True)
            return False
        if gw.publishing:
            await cq.answer(t("join.publishing"), show_alert=True)
            return False
        if await is_blacklisted(session, uid):
            await cq.answer(t("join.banned"), show_alert=True)
            return False
        if await has_participated(session, gid, uid):
            await cq.answer(t("join.already"), show_alert=True)
            return False
        channels = [*(gw.required_channels or []), *(gw.required_groups or [])]
    if channels:
        missing = await missing_channels(cq.bot, uid, channels)
        if missing:
            notice = t(
                "join.not_member", channels="\n".join(display_requirement(raw) for raw in missing)
            )
            if len(notice) <= 190 and not any("|" in raw for raw in missing):
                await cq.answer(notice, show_alert=True)
            else:
                # Telegram回调弹窗最多200字符；完整要求发私聊，避免多群组时按钮报错。
                try:
                    await cq.bot.send_message(
                        uid, t("join.not_member", channels=linked_requirements(missing))
                    )
                    summary = "请先加入必加频道和群组，完整清单已发送到私聊。"
                except TelegramAPIError:
                    summary = "请先私聊机器人发送 /start，再点击参加查看完整的必加频道和群组清单。"
                await cq.answer(summary, show_alert=True)
            return False
    return True


async def _locked_eligibility(session, gid: int, uid: int) -> str | None:
    gw = await claim_giveaway(session, gid, GiveawayStatus.running, require_open=True)
    # The UPDATE might have waited for a database writer across the deadline.
    if gw is None or (gw.draw_mode != "count" and gw.end_at <= datetime.now(UTC)):
        return "join.ended"
    if await is_blacklisted(session, uid):
        return "join.banned"
    if await has_participated(session, gid, uid):
        return "join.already"
    return None


async def _send_question(cq: CallbackQuery, challenge: Challenge) -> bool:
    text, keyboard = question_message(challenge)
    try:
        await cq.bot.send_message(cq.from_user.id, text, reply_markup=keyboard)
    except TelegramAPIError as exc:
        if isinstance(exc, (TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter)):
            await record_delivery(challenge, cq.from_user.id, delivered=False)
        log.warning("无法向用户 %s 发送答题私信", cq.from_user.id)
        key = (
            "join.open_bot_first"
            if isinstance(exc, TelegramForbiddenError)
            else "poetry.send_failed"
        )
        await cq.answer(t(key), show_alert=True)
        return False
    active = await record_delivery(challenge, cq.from_user.id, delivered=True)
    if not active:
        await cq.answer(t("poetry.stale"), show_alert=True)
    return active


async def _report_answer(cq: CallbackQuery, result: str, challenge: Challenge | None) -> None:
    messages = {
        "ok": "join.ok",
        "already": "join.already",
        "locked": "poetry.locked",
        "stale": "poetry.stale",
        "timeout": "poetry.timeout",
        "wrong": "poetry.previous_wrong",
        "replaced": "poetry.replaced",
    }
    if result in {"wrong", "replaced"} and challenge is not None:
        # Record the answer before attempting to send the next question.
        if await _send_question(cq, challenge):
            key = "poetry.replaced" if result == "replaced" else "poetry.wrong"
            await cq.answer(t(key, remaining=challenge.remaining), show_alert=True)
    else:
        await cq.answer(t(messages[result]), show_alert=result != "ok")
    if result != "stale" and getattr(cq, "message", None) and cq.message.chat.id == cq.from_user.id:
        try:
            await cq.message.edit_text(t(messages[result]))
        except TelegramAPIError:
            log.debug("旧答题消息更新失败", exc_info=True)


def build_participate_router() -> Router:
    router = Router(name="participate")

    @router.callback_query(JoinCb.filter())
    async def on_join(cq: CallbackQuery, callback_data: JoinCb) -> None:
        gid, uid = callback_data.giveaway_id, cq.from_user.id
        if not await _eligible(cq, gid):
            return
        async with get_sessionmaker()() as session:
            error = await _locked_eligibility(session, gid, uid)
            if error:
                await cq.answer(t(error), show_alert=True)
                return
            await ensure_user(session, uid, cq.from_user.username)
            challenge = await get_challenge(session, gid, uid)
            await session.commit()
        if challenge is None:
            await cq.answer(t("poetry.locked"), show_alert=True)
        elif await _send_question(cq, challenge):
            await cq.answer(t("poetry.sent"), show_alert=False)

    @router.callback_query(PoetryCb.filter())
    async def on_poetry(cq: CallbackQuery, callback_data: PoetryCb) -> None:
        gid, uid = callback_data.giveaway_id, cq.from_user.id
        if not await _eligible(cq, gid):
            return
        async with winner_delivery_lock(gid):
            async with get_sessionmaker()() as session:
                error = await _locked_eligibility(session, gid, uid)
                if error:
                    await cq.answer(t(error), show_alert=True)
                    return
                result, challenge = await answer(
                    session, gid, uid, cq.from_user.username, callback_data.nonce, callback_data.choice
                )
                draw = await draw_if_full(session, gid) if result == "ok" else None
                await session.commit()
            if draw is not None:
                try:
                    await _report_answer(cq, result, challenge)
                finally:
                    await publish_draw(cq.bot, draw)
                return
        await _report_answer(cq, result, challenge)

    return router
