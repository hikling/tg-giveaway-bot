from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.callbacks.factories import JoinCb, PoetryCb
from bot.db.base import get_sessionmaker
from bot.db.models import GiveawayStatus
from bot.db.repo import claim_giveaway, ensure_user, get_giveaway, has_participated, is_blacklisted
from bot.i18n import t
from bot.poetry import POEMS
from bot.services.channels import missing_channels
from bot.services.poetry import Challenge, answer, get_challenge
from bot.settings import get_settings
from bot.utils.antiabuse import account_age_gate
from bot.utils.esc import esc_list

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
        if gw is None or gw.status != GiveawayStatus.running or gw.end_at <= datetime.now(UTC):
            await cq.answer(t("join.ended"), show_alert=True)
            return False
        if await is_blacklisted(session, uid):
            await cq.answer(t("join.banned"), show_alert=True)
            return False
        if await has_participated(session, gid, uid):
            await cq.answer(t("join.already"), show_alert=True)
            return False
        channels = list(gw.required_channels)
    if channels:
        missing = await missing_channels(cq.bot, uid, channels)
        if missing:
            await cq.answer(t("join.not_member", channels=esc_list(missing)), show_alert=True)
            return False
    return True


async def _locked_eligibility(session, gid: int, uid: int) -> str | None:
    if await claim_giveaway(session, gid, GiveawayStatus.running, require_open=True) is None:
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
    except TelegramAPIError:
        log.warning("无法向用户 %s 发送答题私信", cq.from_user.id)
        await cq.answer(t("join.open_bot_first"), show_alert=True)
        return False
    return True


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
        async with get_sessionmaker()() as session:
            error = await _locked_eligibility(session, gid, uid)
            if error:
                await cq.answer(t(error), show_alert=True)
                return
            result, challenge = await answer(
                session, gid, uid, cq.from_user.username, callback_data.nonce, callback_data.choice
            )
            await session.commit()
        if result == "wrong":
            # 先提交计错，再发下一题；发送失败后可点参加恢复同一道题。
            if await _send_question(cq, challenge):
                await cq.answer(t("poetry.wrong", remaining=challenge.remaining), show_alert=True)
        else:
            key = {
                "ok": "join.ok",
                "already": "join.already",
                "locked": "poetry.locked",
                "stale": "poetry.stale",
            }[result]
            await cq.answer(t(key), show_alert=result != "ok")
        # 私聊中的旧题目给出清晰结果；旧按钮即使移除失败也无法重复消费。
        if result != "stale" and getattr(cq, "message", None) and cq.message.chat.id == uid:
            try:
                await cq.message.edit_text(
                    t(
                        {
                            "ok": "join.ok",
                            "already": "join.already",
                            "locked": "poetry.locked",
                            "wrong": "poetry.previous_wrong",
                        }[result]
                    )
                )
            except TelegramAPIError:
                log.debug("旧答题消息更新失败", exc_info=True)

    return router
