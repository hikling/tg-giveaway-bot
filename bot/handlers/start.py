from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from bot.db.base import get_sessionmaker
from bot.db.repo import ensure_user, me_participations
from bot.filters.admin import is_admin
from bot.handlers.common import lang_of
from bot.i18n import t
from bot.services.admin_menu import set_admin_menu
from bot.settings import get_settings

log = logging.getLogger("bot.handlers.start")


def build_start_router() -> Router:
    router = Router(name="start")

    @router.message(CommandStart())
    async def on_start(message: Message, command: CommandObject, bot: Bot) -> None:
        sm = get_sessionmaker()
        async with sm() as session:
            await ensure_user(session, message.from_user.id, message.from_user.username)
            await session.commit()
        lang = await lang_of(message.from_user.id)
        await message.answer(t("start.welcome", lang))
        settings = get_settings()
        if (
            settings.mini_app_base_url
            and message.chat.type == "private"
            and await is_admin(message.from_user.id, settings.admin_ids)
        ):
            await set_admin_menu(bot, settings, message.from_user.id)

    @router.message(Command("help"))
    async def on_help(message: Message) -> None:
        lang = await lang_of(message.from_user.id)
        await message.answer(t("start.help", lang))

    @router.message(Command("lang"))
    async def on_lang(message: Message) -> None:
        await message.answer("本机器人仅支持中文，无需切换语言。")

    @router.message(Command("me"))
    async def on_me(message: Message) -> None:
        lang = await lang_of(message.from_user.id)
        sm = get_sessionmaker()
        async with sm() as session:
            rows = await me_participations(session, message.from_user.id, limit=20)
        if not rows:
            await message.answer(t("start.me_empty", lang))
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
        await message.answer("\n".join(lines))

    return router
