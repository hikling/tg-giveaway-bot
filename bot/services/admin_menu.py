from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import MenuButtonWebApp, WebAppInfo

from bot.db.base import get_sessionmaker
from bot.db.repo import list_admins
from bot.settings import Settings

log = logging.getLogger(__name__)


async def set_admin_menu(bot: Bot, settings: Settings, user_id: int) -> None:
    if not settings.mini_app_base_url:
        return
    try:
        await bot.set_chat_menu_button(
            chat_id=user_id,
            menu_button=MenuButtonWebApp(
                text="抽奖管理",
                web_app=WebAppInfo(url=settings.mini_app_base_url + "/admin/"),
            ),
        )
    except TelegramAPIError as exc:
        # An administrator may not have opened a private chat with the bot yet.
        log.warning("管理员 %s 设置小程序菜单失败（%s）", user_id, type(exc).__name__)


async def configure_admin_menus(bot: Bot, settings: Settings) -> None:
    if not settings.mini_app_base_url:
        return
    async with get_sessionmaker()() as session:
        ids = dict.fromkeys(
            [*settings.admin_ids, *(row.user_id for row in await list_admins(session))]
        )
    for user_id in ids:
        await set_admin_menu(bot, settings, user_id)
