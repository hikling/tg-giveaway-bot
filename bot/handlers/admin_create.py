from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import Bot, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from bot.filters.admin import IsAdmin
from bot.handlers.common import lang_of
from bot.i18n import t
from bot.keyboards.admin import admin_giveaway_panel
from bot.services import giveaway as gw_service
from bot.services import prize as prize_service
from bot.services.channels import linked_requirements, resolve_destinations, validate_channels
from bot.services.publication import publish_all
from bot.services.scheduler import GiveawayScheduler
from bot.settings import get_settings
from bot.utils.esc import esc_list
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
    waiting_confirm = State()


async def _show_confirmation(message: Message, state: FSMContext, lang: str) -> None:
    data = await state.get_data()
    end_at = datetime.fromisoformat(data["end_at_iso"])
    await state.set_state(GiveawayDraft.waiting_confirm)
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
        )
    )


async def _ask_codes(message: Message, state: FSMContext, lang: str) -> None:
    """逐档录入奖品，完成后直接显示发布确认。"""
    data = await state.get_data()
    tiers = list(data.get("prizes") or [])
    index = int(data.get("codes_index", 0))
    if index >= len(tiers):
        await _show_confirmation(message, state, lang)
        return
    tier = tiers[index]
    if data.get("prize_mode") == "shared":
        await message.answer(t("fsm.enter_codes_shared", lang, tier=tier["name"]))
    else:
        await message.answer(
            t("fsm.enter_codes_unique", lang, tier=tier["name"], count=tier["count"])
        )


def build_admin_create_router() -> Router:
    router = Router(name="admin_create")
    router.message.filter(IsAdmin())

    @router.message(Command("cancel"), StateFilter(GiveawayDraft))
    async def on_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(t("common.cancel", await lang_of(message.from_user.id)))

    @router.message(Command("newgiveaway"))
    async def on_new(message: Message, state: FSMContext) -> None:
        lang = await lang_of(message.from_user.id)
        await state.clear()
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
        if not prizes:
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
    async def step_prizemode(message: Message, state: FSMContext) -> None:
        lang = await lang_of(message.from_user.id)
        text = (message.text or "").strip().lower()
        if text in {"0", "none", "no", "n", "off", "否", "不"}:
            mode = "none"
        elif text in {"1", "unique", "code", "codes", "独立码", "码"}:
            mode = "unique"
        elif text in {"2", "shared", "text", "shared_text", "共享", "共享文本", "文本"}:
            mode = "shared"
        else:
            await message.answer(t("fsm.bad_prizemode", lang))
            return
        await state.update_data(prize_mode=mode, codes_index=0, codes_buffer=[])
        if mode == "none":
            await _show_confirmation(message, state, lang)
            return
        await state.set_state(GiveawayDraft.waiting_codes)
        await _ask_codes(message, state, lang)

    @router.message(GiveawayDraft.waiting_codes)
    async def step_codes(message: Message, state: FSMContext) -> None:
        lang = await lang_of(message.from_user.id)
        data = await state.get_data()
        tiers = list(data.get("prizes") or [])
        index = int(data.get("codes_index", 0))
        if index >= len(tiers):
            await _show_confirmation(message, state, lang)
            return

        tier = tiers[index]
        mode = data.get("prize_mode") or "unique"
        raw = (message.text or "").strip()
        if raw == "-":
            codes: list[str] = []
        elif mode == "shared":
            # The whole message is the prize text: newlines are meaningful.
            codes = [raw]
        else:
            codes = [line.strip() for line in raw.splitlines() if line.strip()]
            if not codes:
                await message.answer(t("fsm.bad_codes", lang))
                return

        needed = int(tier.get("count") or 0)
        if mode == "unique" and codes and len(codes) < needed:
            # Fewer codes than winners is legal - the host can top up with
            # /addcodes before the draw - but it must not be a silent surprise.
            await message.answer(t("fsm.codes_short", lang, count=len(codes), needed=needed))

        buffer = list(data.get("codes_buffer") or [])
        if codes:
            buffer.append([str(tier["name"]), codes])
        await state.update_data(codes_buffer=buffer, codes_index=index + 1)

        if index + 1 < len(tiers):
            await _ask_codes(message, state, lang)
            return
        await _show_confirmation(message, state, lang)

    @router.message(GiveawayDraft.waiting_confirm)
    async def step_confirm(
        message: Message,
        state: FSMContext,
        bot: Bot,
        scheduler: GiveawayScheduler,
    ) -> None:
        lang = await lang_of(message.from_user.id)
        text = (message.text or "").strip().lower()
        if text not in {"确认", "ok"}:
            await message.answer(t("common.cancel", lang))
            await state.clear()
            return

        data = await state.get_data()
        end_at = datetime.fromisoformat(data["end_at_iso"])
        if data.get("draw_mode", "time") != "count" and end_at <= datetime.now(UTC):
            await state.set_state(GiveawayDraft.waiting_duration)
            await message.answer(t("fsm.bad_duration", lang))
            return

        requirements = [*data["channels"], *data.get("groups", [])]
        if not requirements:
            await state.set_state(GiveawayDraft.waiting_channels)
            await message.answer(t("fsm.no_destinations", lang))
            return
        # 发布前重新检查权限并解析真实编号，相同目标的用户名/编号只发布一次。
        try:
            problems = [
                *await validate_channels(bot, data["channels"], expected_type="channel"),
                *await validate_channels(bot, data.get("groups", []), expected_type="group"),
            ]
            targets = await resolve_destinations(bot, requirements)
        except Exception:
            await message.answer(t("fsm.publish_check_failed", lang))
            return
        if problems:
            await message.answer(t("fsm.publish_check_failed", lang) + "\n" + esc_list(problems))
            return

        mode = str(data.get("prize_mode") or "none")
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
            destinations=[(target.chat_id, target.name) for target in targets],
        )

        # Stock is filled before the post goes out, so a winner never presses
        # a claim button that has nothing behind it yet.
        max_claims = prize_service.max_claims_for(mode)
        for tier_name, codes in data.get("codes_buffer") or []:
            if not codes:
                continue
            try:
                await prize_service.add_codes(
                    gw.id, str(tier_name), list(codes), max_claims=max_claims
                )
            except Exception:
                log.exception("add_codes failed: giveaway=%s tier=%s", gw.id, tier_name)

        result = await publish_all(bot, gw.id)
        if result.canceled:
            await message.answer(t("fsm.publish_all_failed", lang))
            await state.clear()
            return
        scheduler.schedule(gw.id, gw.end_at, gw.draw_mode)
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
        await state.clear()

    return router
