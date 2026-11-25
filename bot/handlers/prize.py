from __future__ import annotations

import logging
from html import unescape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from bot.callbacks.factories import ClaimCb, PrizeDraftCb
from bot.db.base import get_sessionmaker
from bot.db.models import GiveawayStatus
from bot.db.prize_repo import CLAIM_ALREADY, CLAIM_EMPTY, CLAIM_INELIGIBLE, CLAIM_OK
from bot.db.repo import get_giveaway, is_blacklisted
from bot.filters.admin import IsAdmin
from bot.handlers.common import answer_notice, lang_of
from bot.i18n import t
from bot.services import prize as prize_service
from bot.services.scheduler import GiveawayScheduler
from bot.utils.esc import telegram_text_length
from bot.utils.prize_input import (
    ACCEPT_TEXTS,
    RESET_TEXTS,
    parse_input,
    review_controls,
    serialize_draft,
    valid_action,
)

log = logging.getLogger("bot.handlers.prize")

_DELIVERY_FAILED = "delivery_failed"
_CLAIM_FEEDBACK = {
    CLAIM_ALREADY: "prize.already",
    CLAIM_INELIGIBLE: "prize.not_winner",
    _DELIVERY_FAILED: "prize.dm_failed",
}


class AddCodesDraft(StatesGroup):
    waiting_codes = State()
    waiting_review = State()


# Giveaways whose creator was already warned that a tier ran dry, so a
# hundred disappointed winners do not produce a hundred DMs.
_warned: set[tuple[int, str]] = set()


async def _warn_creator(bot: Bot, giveaway_id: int, creator_id: int, tier: str) -> None:
    key = (giveaway_id, tier)
    if key in _warned:
        return
    _warned.add(key)
    lang = await lang_of(creator_id)
    try:
        await bot.send_message(
            creator_id,
            t("prize.stock_alert", lang, id=giveaway_id, tier=tier),
        )
    except Exception:
        log.warning("could not warn creator %s about empty stock", creator_id)


async def _send_prize_text(bot: Bot, user_id: int, text: str) -> None:
    # Prize translations contain plain text with HTML-escaped values. Preserve
    # the complete prize as a file if it won't fit in one Telegram message.
    if telegram_text_length(text) > 4096:
        await bot.send_document(
            user_id, BufferedInputFile(unescape(text).encode("utf-8"), filename="prizes.txt")
        )
        return
    await bot.send_message(user_id, text, link_preview_options={"is_disabled": True})


async def _claim_one(
    bot: Bot, lang: str, *, giveaway_id: int, user_id: int, tier: str, creator_id: int
) -> str:
    """Attempt one claim and message the user; returns the outcome status."""
    outcome = await prize_service.claim(giveaway_id, user_id, tier)
    if outcome.status == CLAIM_OK and outcome.code:
        try:
            await _send_prize_text(bot, user_id, t("prize.claimed", lang, code=outcome.code))
        except TelegramAPIError:
            # The committed claim remains available in the winner's private history.
            log.warning("奖品私信发送失败，抽奖 %s，用户 %s", giveaway_id, user_id)
            return _DELIVERY_FAILED
    elif outcome.status == CLAIM_EMPTY:
        try:
            await bot.send_message(user_id, t("prize.empty", lang))
        except TelegramAPIError:
            log.warning("无法向用户 %s 发送奖池缺货提醒", user_id)
        await _warn_creator(bot, giveaway_id, creator_id, tier)
    return outcome.status


def build_prize_router() -> Router:
    """Winner-facing routes: the claim button, /claim and /myprizes."""
    router = Router(name="prize")

    @router.callback_query(ClaimCb.filter())
    async def on_claim_button(cq: CallbackQuery, callback_data: ClaimCb, bot: Bot) -> None:
        uid = cq.from_user.id
        lang = await lang_of(uid)
        gid = callback_data.giveaway_id

        sm = get_sessionmaker()
        async with sm() as session:
            gw = await get_giveaway(session, gid)
            banned = await is_blacklisted(session, uid)
        if gw is None:
            await cq.answer(t("prize.unknown", lang), show_alert=True)
            return
        if gw.status != GiveawayStatus.finished or gw.publishing:
            await cq.answer(t("prize.not_drawn", lang), show_alert=True)
            return
        if banned:
            await cq.answer(t("join.banned", lang), show_alert=True)
            return
        if gw.prize_mode == "none":
            await cq.answer(t("prize.none_mode", lang), show_alert=True)
            return

        tier = prize_service.tier_at(gw.prize_tiers or [], callback_data.tier_index)
        if tier is None:
            await cq.answer(t("prize.unknown", lang), show_alert=True)
            return
        tier_name = str(tier.get("name"))

        status = await _claim_one(
            bot,
            lang,
            giveaway_id=gid,
            user_id=uid,
            tier=tier_name,
            creator_id=gw.creator_id,
        )
        if key := _CLAIM_FEEDBACK.get(status):
            await cq.answer(t(key, lang), show_alert=True)
        else:
            await cq.answer()

    @router.message(Command("claim"))
    async def on_claim_command(
        message: Message, command: CommandObject, bot: Bot, scheduler: GiveawayScheduler
    ) -> None:
        uid = message.from_user.id
        lang = await lang_of(uid)
        try:
            gid = int((command.args or "").strip().split()[0])
        except (ValueError, IndexError):
            await answer_notice(message, t("prize.claim_usage", lang), scheduler)
            return

        sm = get_sessionmaker()
        async with sm() as session:
            gw = await get_giveaway(session, gid)
            banned = await is_blacklisted(session, uid)
        if gw is None:
            await answer_notice(message, t("prize.unknown", lang), scheduler)
            return
        if gw.status != GiveawayStatus.finished or gw.publishing:
            await answer_notice(message, t("prize.not_drawn", lang), scheduler)
            return
        if banned:
            await answer_notice(message, t("join.banned", lang), scheduler)
            return
        if gw.prize_mode == "none":
            await answer_notice(message, t("prize.none_mode", lang), scheduler)
            return

        held = await prize_service.won_tiers(gid, uid, gw.prize_tiers or [])
        if not held:
            await answer_notice(message, t("prize.not_winner", lang), scheduler)
            return

        statuses = set()
        for tier_name in held:
            status = await _claim_one(
                bot,
                lang,
                giveaway_id=gid,
                user_id=uid,
                tier=tier_name,
                creator_id=gw.creator_id,
            )
            statuses.add(status)
        if _DELIVERY_FAILED in statuses:
            await answer_notice(message, t("prize.dm_failed", lang), scheduler)
        elif statuses.isdisjoint({CLAIM_OK, CLAIM_EMPTY}):
            key = "prize.already" if CLAIM_ALREADY in statuses else "prize.not_winner"
            await answer_notice(message, t(key, lang), scheduler)

    @router.message(Command("myprizes"))
    async def on_myprizes(message: Message, bot: Bot, scheduler: GiveawayScheduler) -> None:
        uid = message.from_user.id
        lang = await lang_of(uid)
        if message.chat.type != ChatType.PRIVATE or message.chat.id != uid:
            await answer_notice(message, t("prize.private_history", lang), scheduler)
            return
        rows = await prize_service.claimed_by(uid)
        if not rows:
            await message.answer(t("prize.myprizes_empty", lang))
            return
        lines = [t("prize.myprizes_header", lang, count=len(rows))]
        for row in rows:
            lines.append(
                t(
                    "prize.myprizes_row",
                    lang,
                    id=row.giveaway_id,
                    title=row.giveaway_title,
                    tier=row.tier_name,
                    code=row.code,
                )
            )
        await _send_prize_text(bot, uid, "\n\n".join(lines))

    return router


async def _show_stock_review(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    pending = list(data.get("stock_pending", []))
    count = await prize_service.new_stock_count(data["gid"], data["stock_mode"], pending)
    owner = data.get("prize_draft_owner", message.from_user.id)
    controls = await review_controls(state, owner, "stock")
    await state.set_state(AddCodesDraft.waiting_review)
    text = t(
        "prize.stock_review",
        id=data["gid"],
        tier=data["tier"],
        count=count,
        kind="份共享文本" if data.get("stock_mode") == "shared" else "条独立码",
    )
    if count < len(pending):
        text += "\n\n" + t("prize.stock_duplicates", count=len(pending) - count)
    await message.answer(text, reply_markup=controls)


async def _reset_stock_draft(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(stock_pending=[], prize_review_token=None)
    await state.set_state(AddCodesDraft.waiting_codes)
    await message.answer(t("prize.stock_reset", tier=data["tier"]))


async def _accept_stock(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    gid, tier = int(data["gid"]), data["tier"]
    pending = list(data.get("stock_pending", []))
    async with get_sessionmaker()() as session:
        gw = await get_giveaway(session, gid)
    if gw is None or gw.prize_mode == "none":
        await state.clear()
        await message.answer(t("prize.unknown" if gw is None else "prize.none_mode"))
        return
    added = await prize_service.add_codes(
        gid,
        tier,
        pending,
        max_claims=prize_service.max_claims_for(gw.prize_mode),
    )
    await state.clear()
    text = t("prize.addcodes_ok", id=gid, tier=tier, count=added)
    if added < len(pending):
        text += "\n\n" + t("prize.stock_duplicates", count=len(pending) - added)
    await message.answer(text)


def build_prize_admin_router() -> Router:
    """Admin stock routes: /addcodes and /prizestatus."""
    router = Router(name="prize_admin")
    router.message.filter(IsAdmin())

    @router.message(Command("prizestatus"))
    async def on_prizestatus(
        message: Message, command: CommandObject, scheduler: GiveawayScheduler
    ) -> None:
        lang = await lang_of(message.from_user.id)
        try:
            gid = int((command.args or "").strip().split()[0])
        except (ValueError, IndexError):
            await answer_notice(message, t("prize.status_usage", lang), scheduler)
            return

        stock = await prize_service.stock(gid)
        if not stock:
            await answer_notice(message, t("prize.stock_empty", lang, id=gid), scheduler)
            return
        lines = [t("prize.stock_header", lang, id=gid)]
        for tier in stock.values():
            lines.append(
                t(
                    "prize.stock_row",
                    lang,
                    tier=tier.tier_name,
                    total=tier.total,
                    claimed=tier.claimed,
                    remaining=tier.remaining,
                )
            )
        await answer_notice(message, "\n".join(lines), scheduler)

    @router.message(Command("addcodes"))
    @serialize_draft
    async def on_addcodes(
        message: Message,
        command: CommandObject,
        state: FSMContext,
        scheduler: GiveawayScheduler,
    ) -> None:
        lang = await lang_of(message.from_user.id)
        if message.chat.type != ChatType.PRIVATE:
            await answer_notice(message, t("prize.private_admin", lang), scheduler)
            return
        parts = (command.args or "").split(None, 1)
        if len(parts) < 2:
            await message.answer(t("prize.addcodes_usage", lang))
            return
        try:
            gid = int(parts[0])
        except ValueError:
            await message.answer(t("prize.addcodes_usage", lang))
            return
        tier_name = parts[1].strip()

        sm = get_sessionmaker()
        async with sm() as session:
            gw = await get_giveaway(session, gid)
        if gw is None:
            await message.answer(t("prize.unknown", lang))
            return
        if gw.prize_mode == "none":
            await message.answer(t("prize.none_mode", lang))
            return
        known = [str(tier.get("name")) for tier in (gw.prize_tiers or [])]
        if tier_name not in known:
            await message.answer(
                t(
                    "prize.addcodes_unknown_tier",
                    lang,
                    id=gid,
                    tier=tier_name,
                    tiers=", ".join(known) or "-",
                )
            )
            return

        await state.set_state(AddCodesDraft.waiting_codes)
        await state.update_data(
            gid=gid,
            tier=tier_name,
            stock_mode=gw.prize_mode,
            stock_pending=[],
            prize_draft_owner=message.from_user.id,
            prize_review_token=None,
        )
        await message.answer(t("prize.addcodes_prompt", lang, tier=tier_name))

    @router.message(Command("cancel"), StateFilter(AddCodesDraft))
    @serialize_draft
    async def on_cancel_codes(
        message: Message, state: FSMContext, scheduler: GiveawayScheduler
    ) -> None:
        await state.clear()
        await answer_notice(
            message, t("common.cancel", await lang_of(message.from_user.id)), scheduler
        )

    @router.message(StateFilter(AddCodesDraft.waiting_codes, AddCodesDraft.waiting_review))
    @serialize_draft
    async def step_codes(message: Message, state: FSMContext) -> None:
        current = await state.get_state()
        if current not in {AddCodesDraft.waiting_codes.state, AddCodesDraft.waiting_review.state}:
            return
        raw = (message.text or "").strip()
        if raw in RESET_TEXTS:
            await _reset_stock_draft(message, state)
            return
        if raw == "-":
            await state.clear()
            await message.answer(t("common.cancel"))
            return
        if current == AddCodesDraft.waiting_review.state and raw.lower() in ACCEPT_TEXTS:
            await _accept_stock(message, state)
            return
        if not raw:
            await message.answer(t("prize.addcodes_empty"))
            return
        data = await state.get_data()
        codes = parse_input(raw, data["stock_mode"], data.get("stock_pending", []))
        await state.update_data(stock_pending=codes, prize_draft_owner=message.from_user.id)
        await _show_stock_review(message, state)

    @router.callback_query(PrizeDraftCb.filter(F.scope == "stock"))
    @serialize_draft
    async def on_stock_action(
        cq: CallbackQuery, callback_data: PrizeDraftCb, state: FSMContext
    ) -> None:
        if not await IsAdmin()(cq):
            await cq.answer(t("common.not_admin"), show_alert=True)
            return
        current = await state.get_state()
        if (
            not await valid_action(state, cq.from_user.id, callback_data)
            or current
            not in {AddCodesDraft.waiting_codes.state, AddCodesDraft.waiting_review.state}
            or cq.message is None
        ):
            await cq.answer(t("prize.draft_stale"), show_alert=True)
            return
        if callback_data.action == "accept" and current != AddCodesDraft.waiting_review.state:
            await cq.answer(t("prize.draft_stale"), show_alert=True)
            return
        if callback_data.action == "reset":
            await _reset_stock_draft(cq.message, state)
        else:
            await _accept_stock(cq.message, state)
        await cq.answer()

    return router
