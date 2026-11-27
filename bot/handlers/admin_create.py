from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.callbacks.factories import PrizeDraftCb
from bot.db.models import Giveaway, GiveawayStatus
from bot.filters.admin import IsAdmin
from bot.handlers.common import answer_notice, lang_of
from bot.i18n import t
from bot.keyboards.admin import admin_giveaway_panel
from bot.services import giveaway as gw_service
from bot.services.channels import (
    linked_requirements,
    resolve_publication_targets,
    validate_channels,
)
from bot.services.publication import publish_created
from bot.services.scheduler import GiveawayScheduler
from bot.settings import get_settings
from bot.utils.esc import Raw, esc_list
from bot.utils.prize_input import (
    ACCEPT_TEXTS,
    RESET_TEXTS,
    parse_input,
    review_controls,
    serialize_draft,
    valid_action,
)
from bot.utils.timeparse import (
    format_draw_condition,
    parse_channels,
    parse_draw_condition,
    parse_prizes,
)

log = logging.getLogger("bot.handlers.admin_create")


class GiveawayDraft(StatesGroup):
    waiting_title = State()
    waiting_description = State()
    waiting_prizes = State()
    waiting_channels = State()
    waiting_groups = State()
    waiting_duration = State()
    waiting_prizemode = State()
    waiting_codes = State()
    waiting_codes_review = State()
    waiting_confirm = State()


async def _show_confirmation(message: Message, state: FSMContext, lang: str) -> None:
    data = await state.get_data()
    end_at = datetime.fromisoformat(data["end_at_iso"])
    await state.set_state(GiveawayDraft.waiting_confirm)
    owner = data.get("prize_draft_owner", message.from_user.id)
    controls = (
        await review_controls(state, owner, "new", accept=False)
        if data.get("prize_mode") != "none"
        else None
    )
    await message.answer(
        t(
            "fsm.confirm",
            lang,
            title=data.get("title", ""),
            prizes="\n".join(f"• {p['name']} × {p['count']}" for p in data.get("prizes", [])),
            channels=linked_requirements(data.get("channels", [])),
            groups=linked_requirements(data.get("groups", [])),
            ends_at=format_draw_condition(
                end_at,
                data.get("draw_mode", "time"),
                data.get("target_participants"),
                get_settings().tz,
            ),
            prizemode=t(f"fsm.mode_{data.get('prize_mode', 'none')}", lang),
            inventory=_inventory_summary(data, lang),
        ),
        reply_markup=controls,
    )


async def _ask_codes(message: Message, state: FSMContext, lang: str) -> None:
    """逐档录入奖品，完成后直接显示发布确认。"""
    data = await state.get_data()
    tiers = list(data.get("prizes") or [])
    index = int(data.get("codes_index", 0))
    if index >= len(tiers):
        await _show_confirmation(message, state, lang)
        return
    await state.set_state(GiveawayDraft.waiting_codes)
    # 进入下一档使前一档的旧按钮立即失效。
    owner = data.get("prize_draft_owner", message.from_user.id)
    controls = await review_controls(state, owner, "new", accept=False)
    tier = tiers[index]
    await message.answer(
        t("fsm.enter_codes_unique", lang, tier=tier["name"], count=tier["count"]),
        reply_markup=controls,
    )


def _inventory_summary(data: dict, lang: str) -> str:
    if data.get("prize_mode") == "none":
        return t("fsm.inventory_manual", lang)
    counts = {name: len(codes) for name, codes in data.get("codes_buffer", [])}
    lines = [t("fsm.inventory_total", lang, count=sum(counts.values()))]
    for tier in data.get("prizes", []):
        lines.append(
            t(
                "fsm.inventory_row",
                lang,
                tier=tier["name"],
                count=counts.get(tier["name"], 0),
                needed=tier["count"],
            )
        )
    return Raw("\n".join(lines))


async def _reset_all_codes(message: Message, state: FSMContext, lang: str) -> None:
    data = await state.get_data()
    if (request_id := data.get("request_id")) and (
        await gw_service.get_creation(message.from_user.id, request_id)
    ):
        await message.answer(t("fsm.confirm_hint", lang))
        return
    if data.get("prize_mode") == "none":
        await message.answer(t("fsm.inventory_manual", lang))
        await _show_confirmation(message, state, lang)
        return
    await state.update_data(
        codes_buffer=[], codes_pending=[], codes_index=0, prize_review_token=None
    )
    await message.answer(t("fsm.codes_reset", lang))
    await _ask_codes(message, state, lang)


async def _review_codes(message: Message, state: FSMContext, lang: str) -> None:
    data = await state.get_data()
    tier = data["prizes"][int(data["codes_index"])]
    count, needed = len(data.get("codes_pending", [])), int(tier["count"])
    difference = (
        f"少 {needed - count} 条"
        if count < needed
        else f"多 {count - needed} 条"
        if count > needed
        else "数量一致"
    )
    owner = data.get("prize_draft_owner", message.from_user.id)
    controls = await review_controls(state, owner, "new")
    await state.set_state(GiveawayDraft.waiting_codes_review)
    await message.answer(
        t(
            "fsm.codes_review",
            lang,
            tier=tier["name"],
            count=count,
            needed=needed,
            difference=difference,
            total=sum(len(codes) for _, codes in data.get("codes_buffer", [])) + count,
        ),
        reply_markup=controls,
    )


async def _accept_codes(message: Message, state: FSMContext, lang: str) -> None:
    data = await state.get_data()
    index = int(data["codes_index"])
    tier = data["prizes"][index]
    buffer = list(data.get("codes_buffer", []))
    pending = list(data.get("codes_pending", []))
    if data.get("prize_mode") == "unique":
        prior = {code for _, codes in buffer for code in codes}
        if prior.intersection(pending):
            await message.answer(t("prize.duplicate_tiers", lang))
            return
    # 当前档只写入一次；重复按钮会被新的凭据和状态拒绝。
    buffer.append([str(tier["name"]), pending])
    await state.update_data(
        codes_buffer=buffer, codes_pending=[], codes_index=index + 1, prize_review_token=None
    )
    await _ask_codes(message, state, lang)


async def _save_draft(
    message: Message, state: FSMContext, bot: Bot, lang: str, data: dict, request_id: str
) -> Giveaway | None:
    end_at = datetime.fromisoformat(data["end_at_iso"])
    if data.get("draw_mode", "time") != "count" and end_at <= datetime.now(UTC):
        await state.set_state(GiveawayDraft.waiting_duration)
        await message.answer(t("fsm.bad_duration", lang))
        return None

    requirements = [*data["channels"], *data.get("groups", [])]
    if not requirements:
        await state.set_state(GiveawayDraft.waiting_channels)
        await message.answer(t("fsm.no_destinations", lang))
        return None
    # 发布前重新检查权限并解析真实编号，相同目标的用户名/编号只发布一次。
    try:
        targets = await resolve_publication_targets(
            bot, data["channels"], data.get("groups", [])
        )
    except ValueError as exc:
        await message.answer(t("fsm.publish_check_failed", lang) + "\n" + esc_list([str(exc)]))
        return None
    except Exception:
        await message.answer(t("fsm.publish_check_failed", lang))
        return None
    mode = str(data.get("prize_mode") or "none")
    try:
        gw = await gw_service.create(
            creator_id=message.from_user.id,
            title=data["title"],
            description=data["description"],
            prize_tiers=data["prizes"],
            required_channels=data["channels"],
            required_groups=data.get("groups", []),
            draw_mode=data.get("draw_mode", "time"),
            target_participants=data.get("target_participants"),
            end_at=end_at,
            turnstile_required=False,
            prize_mode=mode,
            publishing=True,
            initial_codes=[
                (str(name), list(codes)) for name, codes in data.get("codes_buffer", [])
            ],
            destinations=[(target.chat_id, target.name) for target in targets],
            request_id=request_id,
        )
    except Exception as exc:
        log.error("抽奖与奖品事务保存失败：%s", type(exc).__name__)
        await message.answer(t("fsm.stock_save_failed", lang))
        return None
    return gw


def build_admin_create_router() -> Router:
    router = Router(name="admin_create")
    router.message.filter(IsAdmin())

    @router.message(Command("cancel"), StateFilter(GiveawayDraft))
    @serialize_draft
    async def on_cancel(message: Message, state: FSMContext, scheduler: GiveawayScheduler) -> None:
        await state.clear()
        await answer_notice(
            message, t("common.cancel", await lang_of(message.from_user.id)), scheduler
        )

    @router.message(Command("newgiveaway"))
    @serialize_draft
    async def on_new(message: Message, state: FSMContext, scheduler: GiveawayScheduler) -> None:
        lang = await lang_of(message.from_user.id)
        if message.chat.type != ChatType.PRIVATE:
            await answer_notice(message, t("prize.private_admin", lang), scheduler)
            return
        await state.clear()
        await state.update_data(prize_draft_owner=message.from_user.id)
        await state.set_state(GiveawayDraft.waiting_title)
        await message.answer(t("fsm.enter_title", lang))

    @router.message(GiveawayDraft.waiting_title)
    async def step_title(message: Message, state: FSMContext) -> None:
        lang = await lang_of(message.from_user.id)
        title = (message.text or "").strip()
        if not title:
            await message.answer(t("fsm.enter_title", lang))
            return
        await state.update_data(title=title[:256])
        await state.set_state(GiveawayDraft.waiting_description)
        await message.answer(t("fsm.enter_description", lang))

    @router.message(GiveawayDraft.waiting_description)
    async def step_description(message: Message, state: FSMContext) -> None:
        lang = await lang_of(message.from_user.id)
        text = (message.text or "").strip()
        description = "" if text == "-" else text[:4096]
        await state.update_data(description=description)
        await state.set_state(GiveawayDraft.waiting_prizes)
        await message.answer(t("fsm.enter_prizes", lang))

    @router.message(GiveawayDraft.waiting_prizes)
    async def step_prizes(message: Message, state: FSMContext) -> None:
        lang = await lang_of(message.from_user.id)
        prizes = parse_prizes(message.text or "")
        if (
            not prizes
            or len({p["name"] for p in prizes}) != len(prizes)
            or any(len(p["name"]) > 64 for p in prizes)
        ):
            await message.answer(t("fsm.bad_prizes", lang))
            return
        await state.update_data(prizes=prizes)
        await state.set_state(GiveawayDraft.waiting_channels)
        await message.answer(t("fsm.enter_channels", lang))

    @router.message(GiveawayDraft.waiting_channels)
    async def step_channels(message: Message, state: FSMContext, bot: Bot) -> None:
        lang = await lang_of(message.from_user.id)
        try:
            channels = parse_channels(message.text or "")
        except ValueError:
            await message.answer(t("fsm.bad_chat_ref", lang))
            return
        if channels:
            # A channel the bot cannot read would make membership checks
            # fail closed for everybody (TelegramAPIError -> "not a member"),
            # so the giveaway could never be won. Catch it at creation.
            missing = await validate_channels(bot, channels, expected_type="channel")
            if missing:
                await message.answer(t("fsm.bad_channels", lang, channels=esc_list(missing)))
                return
        await state.update_data(channels=channels)
        await state.set_state(GiveawayDraft.waiting_groups)
        await message.answer(t("fsm.enter_groups", lang))

    @router.message(GiveawayDraft.waiting_groups)
    async def step_groups(message: Message, state: FSMContext, bot: Bot) -> None:
        lang = await lang_of(message.from_user.id)
        try:
            groups = parse_channels(message.text or "")
        except ValueError:
            await message.answer(t("fsm.bad_chat_ref", lang))
            return
        missing = await validate_channels(bot, groups, expected_type="group")
        if missing:
            await message.answer(t("fsm.bad_groups", lang, groups=esc_list(missing)))
            return
        data = await state.get_data()
        if not groups and not data.get("channels"):
            await message.answer(t("fsm.no_destinations", lang))
            return
        await state.update_data(groups=groups)
        await state.set_state(GiveawayDraft.waiting_duration)
        await message.answer(t("fsm.enter_duration", lang))

    @router.message(GiveawayDraft.waiting_duration)
    async def step_duration(message: Message, state: FSMContext) -> None:
        lang = await lang_of(message.from_user.id)
        tz = get_settings().tz
        condition = parse_draw_condition(message.text or "", tz)
        if condition is None or (condition[0] != "count" and condition[1] <= datetime.now(UTC)):
            await message.answer(t("fsm.bad_duration", lang))
            return
        mode, end_at, target = condition
        await state.update_data(
            end_at_iso=end_at.isoformat(), draw_mode=mode, target_participants=target
        )
        await state.set_state(GiveawayDraft.waiting_prizemode)
        await message.answer(t("fsm.enter_prizemode", lang))

    @router.message(GiveawayDraft.waiting_prizemode)
    @serialize_draft
    async def step_prizemode(message: Message, state: FSMContext) -> None:
        lang = await lang_of(message.from_user.id)
        text = (message.text or "").strip().lower()
        if text in {"0", "none", "no", "n", "off", "否", "不"}:
            mode = "none"
        elif text in {"1", "unique", "code", "codes", "独立码", "码"}:
            mode = "unique"
        else:
            await message.answer(t("fsm.bad_prizemode", lang))
            return
        await state.update_data(
            prize_mode=mode,
            codes_index=0,
            codes_buffer=[],
            codes_pending=[],
            prize_draft_owner=message.from_user.id,
        )
        if mode == "none":
            await _show_confirmation(message, state, lang)
            return
        await state.set_state(GiveawayDraft.waiting_codes)
        await _ask_codes(message, state, lang)

    @router.message(StateFilter(GiveawayDraft.waiting_codes, GiveawayDraft.waiting_codes_review))
    @serialize_draft
    async def step_codes(message: Message, state: FSMContext) -> None:
        current = await state.get_state()
        if current not in {
            GiveawayDraft.waiting_codes.state,
            GiveawayDraft.waiting_codes_review.state,
        }:
            return
        lang = await lang_of(message.from_user.id)
        raw = (message.text or "").strip()
        if raw in RESET_TEXTS:
            await _reset_all_codes(message, state, lang)
            return
        if current == GiveawayDraft.waiting_codes_review.state and raw.lower() in ACCEPT_TEXTS:
            await _accept_codes(message, state, lang)
            return
        if not raw:
            await message.answer(t("fsm.bad_codes", lang))
            return
        data = await state.get_data()
        codes = parse_input(raw, data.get("prize_mode", "unique"), data.get("codes_pending", []))
        await state.update_data(codes_pending=codes, prize_draft_owner=message.from_user.id)
        await _review_codes(message, state, lang)

    @router.callback_query(PrizeDraftCb.filter(F.scope == "new"))
    @serialize_draft
    async def on_codes_action(
        cq: CallbackQuery, callback_data: PrizeDraftCb, state: FSMContext
    ) -> None:
        if not await IsAdmin()(cq):
            await cq.answer(t("common.not_admin"), show_alert=True)
            return
        current = await state.get_state()
        if not await valid_action(state, cq.from_user.id, callback_data) or current not in {
            GiveawayDraft.waiting_codes.state,
            GiveawayDraft.waiting_codes_review.state,
            GiveawayDraft.waiting_confirm.state,
        }:
            await cq.answer(t("prize.draft_stale"), show_alert=True)
            return
        if callback_data.action == "accept" and current != GiveawayDraft.waiting_codes_review.state:
            await cq.answer(t("prize.draft_stale"), show_alert=True)
            return
        if cq.message is None:
            await cq.answer(t("prize.draft_stale"), show_alert=True)
            return
        if callback_data.action == "reset":
            await _reset_all_codes(cq.message, state, "zh")
        else:
            await _accept_codes(cq.message, state, "zh")
        await cq.answer()

    @router.message(GiveawayDraft.waiting_confirm)
    @serialize_draft
    async def step_confirm(
        message: Message,
        state: FSMContext,
        bot: Bot,
        scheduler: GiveawayScheduler,
    ) -> None:
        if await state.get_state() != GiveawayDraft.waiting_confirm.state:
            return
        lang = await lang_of(message.from_user.id)
        text = (message.text or "").strip()
        if text in RESET_TEXTS:
            await _reset_all_codes(message, state, lang)
            return
        if text.lower() not in {"确认", "ok"}:
            await message.answer(t("fsm.confirm_hint", lang))
            return

        data = await state.get_data()
        request_id = data.get("request_id")
        if not request_id:
            request_id = str(uuid4())
            await state.update_data(request_id=request_id)
        gw = await gw_service.get_creation(message.from_user.id, request_id)
        if gw is None:
            gw = await _save_draft(message, state, bot, lang, data, request_id)
            if gw is None:
                return

        try:
            result = await publish_created(bot, gw)
        except Exception as exc:
            log.error("抽奖 %s 发布未确认：%s", gw.id, type(exc).__name__)
            await message.answer(t("fsm.publish_retry_saved", lang, id=gw.id))
            return
        if gw.status == GiveawayStatus.running and not result.canceled:
            scheduler.schedule(gw.id, gw.end_at, gw.draw_mode)
        await state.clear()
        if result.canceled:
            await message.answer(t("fsm.publish_all_failed", lang))
            return
        if result.failed:
            await message.answer(
                t(
                    "fsm.publish_partial",
                    lang,
                    id=gw.id,
                    sent=result.sent,
                    names="、".join(result.failed),
                )
            )

        # Admin controls stay in the admin chat on purpose: the public post
        # only carries the join button.
        await message.answer(t("fsm.saved", lang), reply_markup=admin_giveaway_panel(gw.id))

    return router
