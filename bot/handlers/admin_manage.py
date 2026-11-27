from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from bot.callbacks.factories import AdminGwCb
from bot.db.base import get_sessionmaker
from bot.db.models import GiveawayStatus
from bot.db.repo import (
    add_admin,
    ban_user,
    count_participants,
    delete_participations,
    list_admins,
    list_giveaways_by_creator,
    list_participants,
    unban_user,
)
from bot.filters.admin import IsAdmin, is_admin
from bot.handlers.common import answer_notice, lang_of
from bot.i18n import t
from bot.services import giveaway as gw_service
from bot.services.admin_menu import set_admin_menu
from bot.services.notifier import retry_pending
from bot.services.scheduler import GiveawayScheduler
from bot.settings import get_settings
from bot.utils.esc import esc, user_mention
from bot.utils.ids import parse_id
from bot.utils.timeparse import format_draw_condition

log = logging.getLogger("bot.handlers.admin_manage")


def _int_arg(text: str) -> int | None:
    """First whitespace-separated token as an int, or None."""
    head = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return parse_id(head)


def build_admin_manage_router() -> Router:
    router = Router(name="admin_manage")
    router.message.filter(IsAdmin())

    @router.callback_query(AdminGwCb.filter())
    async def on_panel(
        cq: CallbackQuery,
        callback_data: AdminGwCb,
        bot: Bot,
        scheduler: GiveawayScheduler,
    ) -> None:
        # admin_giveaway_panel() and this factory shipped with no handler at
        # all, so the cancel button raised a callback timeout. Note that the
        # message filter above is message-only: aiogram does not carry it over
        # to callbacks, hence the explicit re-check.
        if not await is_admin(cq.from_user.id, get_settings().admin_ids):
            lang = await lang_of(cq.from_user.id)
            await cq.answer(t("common.not_admin", lang), show_alert=True)
            return
        if callback_data.action != "cancel":
            await cq.answer()
            return
        lang = await lang_of(cq.from_user.id)
        gid = callback_data.giveaway_id
        if await gw_service.cancel(gid):
            scheduler.cancel(gid)
            await cq.answer(t("admin.cancel_ok", lang, id=gid))
        else:
            await cq.answer(t("admin.cancel_missing", lang, id=gid), show_alert=True)

    @router.message(Command("list"))
    async def on_list(message: Message, scheduler: GiveawayScheduler) -> None:
        lang = await lang_of(message.from_user.id)
        sm = get_sessionmaker()
        async with sm() as session:
            # creator_id=None: every admin sees every running giveaway, which
            # is what the README promised and what /cancel + /reroll act on.
            running = await list_giveaways_by_creator(session, status=GiveawayStatus.running)
            counts = {gw.id: await count_participants(session, gw.id) for gw in running}
        if not running:
            await answer_notice(message, t("admin.list_empty", lang), scheduler)
            return
        tz = get_settings().tz
        lines = [t("admin.list_header", lang, count=len(running))]
        for gw in running:
            lines.append(
                t(
                    "admin.list_row",
                    lang,
                    id=gw.id,
                    title=gw.title,
                    ends_at=format_draw_condition(
                        gw.end_at, gw.draw_mode, gw.target_participants, tz
                    ),
                    count=counts[gw.id],
                )
            )
        await answer_notice(message, "\n".join(lines), scheduler)

    @router.message(Command("cancel"))
    async def on_cancel(
        message: Message, command: CommandObject, scheduler: GiveawayScheduler
    ) -> None:
        lang = await lang_of(message.from_user.id)
        if not command.args:
            await answer_notice(message, t("admin.usage_cancel", lang), scheduler)
            return
        gid = _int_arg(command.args)
        if gid is None:
            await answer_notice(message, t("admin.usage_cancel", lang), scheduler)
            return
        ok = await gw_service.cancel(gid)
        if ok:
            scheduler.cancel(gid)
            await answer_notice(message, t("admin.cancel_ok", lang, id=gid), scheduler)
        else:
            await answer_notice(message, t("admin.cancel_missing", lang, id=gid), scheduler)

    @router.message(Command("reroll"))
    async def on_reroll(
        message: Message, command: CommandObject, bot: Bot, scheduler: GiveawayScheduler
    ) -> None:
        lang = await lang_of(message.from_user.id)
        if not command.args:
            await answer_notice(message, t("admin.usage_reroll", lang), scheduler)
            return
        parts = command.args.split(maxsplit=1)
        gid = parse_id(parts[0]) if parts else None
        if len(parts) < 2 or gid is None:
            await answer_notice(message, t("admin.usage_reroll", lang), scheduler)
            return
        tier = parts[1].strip()
        try:
            picks, shortfall = await gw_service.reroll_tier(gid, tier, bot)
        except gw_service.RerollError as exc:
            key = {
                "running": "admin.reroll_running",
                "canceled": "admin.reroll_canceled",
                "no_such_tier": "admin.reroll_no_tier",
                "claimed": "admin.reroll_claimed",
            }.get(str(exc), "admin.reroll_empty")
            await answer_notice(message, t(key, lang, id=gid, tier=esc(tier)), scheduler)
            return
        if not picks:
            await answer_notice(
                message, t("admin.reroll_empty", lang, id=gid, tier=esc(tier)), scheduler
            )
            return
        await answer_notice(
            message, t("admin.reroll_ok", lang, id=gid, tier=esc(tier), count=len(picks)), scheduler
        )

    @router.message(Command("gate"))
    async def on_gate(
        message: Message, command: CommandObject, scheduler: GiveawayScheduler
    ) -> None:
        """Show the anti-abuse policy, and optionally the ID spread of a giveaway.

        Picking a MIN_USER_ID blind is how people block their whole audience, so
        this prints the participant ID distribution first.
        """
        lang = await lang_of(message.from_user.id)
        settings = get_settings()
        lines = [
            t(
                "admin.gate_header",
                lang,
                gate=("开启" if settings.account_age_gate else "关闭"),
                min_user_id=settings.min_user_id,
            )
        ]
        gid = _int_arg(command.args or "")
        if gid is not None:
            sm = get_sessionmaker()
            async with sm() as session:
                people = await list_participants(session, gid, verified_only=True)
            ids = sorted(p.user_id for p in people)
            if ids:
                below = (
                    sum(1 for i in ids if i < settings.min_user_id) if settings.min_user_id else 0
                )
                lines.append(
                    t(
                        "admin.gate_dist",
                        lang,
                        id=gid,
                        n=len(ids),
                        low=ids[0],
                        mid=ids[len(ids) // 2],
                        high=ids[-1],
                        blocked=below,
                    )
                )
            else:
                lines.append(t("admin.gate_empty", lang, id=gid))
        await answer_notice(message, "\n".join(lines), scheduler)

    @router.message(Command("ban"))
    async def on_ban(
        message: Message, command: CommandObject, bot: Bot, scheduler: GiveawayScheduler
    ) -> None:
        lang = await lang_of(message.from_user.id)
        if not command.args:
            await answer_notice(message, t("admin.usage_ban", lang), scheduler)
            return
        parts = command.args.split(maxsplit=1)
        uid = _int_arg(parts[0])
        if uid is None:
            await answer_notice(message, t("admin.usage_ban", lang), scheduler)
            return
        reason = parts[1].strip() if len(parts) > 1 else None
        sm = get_sessionmaker()
        async with sm() as session:
            await ban_user(session, uid, reason=reason, banned_by=message.from_user.id)
            # P1-3: the ban used to be enforced only at join time, so a user
            # blacklisted mid-giveaway kept their seat and could still win.
            removed = await delete_participations(session, uid)
            await session.commit()
        if removed:
            from bot.services.publication import schedule_running_post_counts_sync

            await schedule_running_post_counts_sync(bot)
        key = "admin.ban_ok" if removed else "admin.ban_ok_none"
        await answer_notice(message, t(key, lang, user_id=uid, n=removed), scheduler)

    @router.message(Command("republish"))
    async def on_republish(
        message: Message, command: CommandObject, bot: Bot, scheduler: GiveawayScheduler
    ) -> None:
        from bot.services.publication import publish_all

        gid = _int_arg(command.args or "")
        if gid is None:
            await answer_notice(message, "用法：/republish 〈抽奖编号〉", scheduler)
            return
        try:
            result = await publish_all(bot, gid)
        except ValueError:
            await answer_notice(message, "抽奖不存在、已结束或已取消，无法补发。", scheduler)
            return
        await answer_notice(
            message,
            t(
                "fsm.publish_partial",
                id=gid,
                sent=result.sent,
                names="、".join(result.failed) or "无",
            ),
            scheduler,
        )

    @router.message(Command("retry"))
    async def on_retry(
        message: Message, command: CommandObject, bot: Bot, scheduler: GiveawayScheduler
    ) -> None:
        lang = await lang_of(message.from_user.id)
        gid = _int_arg(command.args or "")
        if gid is None or gid <= 0:
            await answer_notice(message, t("admin.usage_retry", lang), scheduler)
            return
        count = await retry_pending(bot, gid)
        await answer_notice(message, t("admin.retry_ok", lang, id=gid, count=count), scheduler)

    @router.message(Command("unban"))
    async def on_unban(
        message: Message, command: CommandObject, scheduler: GiveawayScheduler
    ) -> None:
        lang = await lang_of(message.from_user.id)
        uid = _int_arg(command.args or "")
        if uid is None:
            await answer_notice(message, t("admin.usage_unban", lang), scheduler)
            return
        sm = get_sessionmaker()
        async with sm() as session:
            await unban_user(session, uid)
            await session.commit()
        await answer_notice(message, t("admin.unban_ok", lang, user_id=uid), scheduler)

    @router.message(Command("addadmin"))
    async def on_addadmin(
        message: Message, command: CommandObject, bot: Bot, scheduler: GiveawayScheduler
    ) -> None:
        lang = await lang_of(message.from_user.id)
        uid = _int_arg(command.args or "")
        if uid is None:
            await answer_notice(message, t("admin.usage_addadmin", lang), scheduler)
            return
        sm = get_sessionmaker()
        async with sm() as session:
            await add_admin(session, uid, added_by=message.from_user.id)
            await session.commit()
        await answer_notice(
            message, t("admin.addadmin_ok", lang, user_id=user_mention(uid)), scheduler
        )
        await set_admin_menu(bot, get_settings(), uid)

    @router.message(Command("admins"))
    async def on_admins(message: Message, scheduler: GiveawayScheduler) -> None:
        # Reuses the AdminGwCb panel entry point that used to be dead code.
        lang = await lang_of(message.from_user.id)
        sm = get_sessionmaker()
        async with sm() as session:
            admins = await list_admins(session)
        if not admins:
            await answer_notice(message, t("admin.list_empty", lang), scheduler)
            return
        lines = [t("admin.list_header", lang, count=len(admins))]
        for row in admins:
            lines.append("• " + user_mention(row.user_id))
        await answer_notice(message, "\n".join(lines), scheduler)

    return router
