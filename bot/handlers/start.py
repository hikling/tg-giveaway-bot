from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.db.base import get_sessionmaker
from bot.db.repo import ensure_user, me_participations
from bot.filters.admin import is_admin
from bot.handlers.common import answer_notice, lang_of
from bot.i18n import t
from bot.services.admin_menu import set_admin_menu
from bot.services.scheduler import GiveawayScheduler
from bot.settings import get_settings


def build_start_router() -> Router:
    router = Router(name="start")

    @router.message(CommandStart())
    async def on_start(message: Message, bot: Bot, scheduler: GiveawayScheduler) -> None:
        sm = get_sessionmaker()
        async with sm() as session:
            await ensure_user(session, message.from_user.id, message.from_user.username)
            await session.commit()
        lang = await lang_of(message.from_user.id)
        await answer_notice(message, t("start.welcome", lang), scheduler)
        settings = get_settings()
        if (
            settings.mini_app_base_url
            and message.chat.type == "private"
            and await is_admin(message.from_user.id, settings.admin_ids)
        ):
            await set_admin_menu(bot, settings, message.from_user.id)

    @router.message(Command("help"))
    async def on_help(message: Message, scheduler: GiveawayScheduler) -> None:
        lang = await lang_of(message.from_user.id)
        await answer_notice(message, t("start.help", lang), scheduler)

    @router.message(Command("lang"))
    async def on_lang(message: Message, scheduler: GiveawayScheduler) -> None:
        await answer_notice(message, "本机器人仅支持中文，无需切换语言。", scheduler)

    @router.message(Command("me"))
    async def on_me(message: Message, scheduler: GiveawayScheduler) -> None:
        lang = await lang_of(message.from_user.id)
        sm = get_sessionmaker()
        async with sm() as session:
            rows = await me_participations(session, message.from_user.id, limit=20)
        if not rows:
            await answer_notice(message, t("start.me_empty", lang), scheduler)
            return
        lines = [t("start.me_header", lang, count=len(rows))]
        for _participant, gw in rows:
            lines.append(
                t(
                    "start.me_row",
                    lang,
                    id=gw.id,
                    title=gw.title,
                    status={"running": "进行中", "finished": "已结束", "canceled": "已取消"}.get(
                        gw.status.value if hasattr(gw.status, "value") else gw.status, "未知"
                    ),
                )
            )
        await answer_notice(message, "\n".join(lines), scheduler)

    return router
