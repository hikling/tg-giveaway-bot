from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from bot.db.base import get_sessionmaker
from bot.db.repo import is_admin_db
from bot.settings import get_settings


async def is_admin(user_id: int, configured_ids: list[int]) -> bool:
    if user_id in configured_ids:
        return True
    async with get_sessionmaker()() as session:
        return await is_admin_db(session, user_id)


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message | CallbackQuery) -> bool:
        uid = message.from_user.id if message.from_user else 0
        return await is_admin(uid, get_settings().admin_ids)
