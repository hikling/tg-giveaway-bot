from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.enums import ChatType
from aiogram.filters import CommandObject
from aiogram.types import Message, TelegramObject

from bot.services.scheduler import GiveawayScheduler

_GROUP_CHAT_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}


async def lang_of(user_id: int) -> str:
    return "zh"


async def schedule_command_deletion(
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: dict[str, Any],
) -> Any:
    # Inner middleware runs only after a command and its permission filters match.
    if (
        isinstance(event, Message)
        and event.chat.type in _GROUP_CHAT_TYPES
        and isinstance(data.get("command"), CommandObject)
    ):
        scheduler: GiveawayScheduler = data["scheduler"]
        scheduler.schedule_group_message_deletion(event.chat.id, event.message_id)
    return await handler(event, data)


async def answer_notice(message: Message, text: str, scheduler: GiveawayScheduler) -> None:
    sent = await message.answer(text)
    if message.chat.type in _GROUP_CHAT_TYPES:
        scheduler.schedule_group_message_deletion(sent.chat.id, sent.message_id)
