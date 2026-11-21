"""奖品计数与入库使用同一套去空行、去重规则。"""

from __future__ import annotations

import asyncio
import inspect
import secrets
import weakref
from collections.abc import Iterable
from functools import wraps

from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.callbacks.factories import PrizeDraftCb

RESET_TEXTS = {"全部删除并重新添加", "全部删除", "清空奖品", "重新录入奖品"}
ACCEPT_TEXTS = {"确认", "确认奖品", "确认本档奖品", "确认入库", "ok"}
_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def normalize_codes(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def parse_input(text: str, mode: str, previous: Iterable[str] = ()) -> list[str]:
    raw = text.strip()
    if raw == "-":
        return []
    if not raw:
        return list(previous)
    if mode == "shared":
        return [raw]
    return normalize_codes([*previous, *raw.splitlines()])


def serialize_draft(func):
    """串行处理同一FSM草稿，防止双击确认重复入库或与清空操作交错。"""
    signature = inspect.signature(func)

    @wraps(func)
    async def wrapped(*args, **kwargs):
        state = signature.bind(*args, **kwargs).arguments["state"]
        key = (asyncio.get_running_loop(), state.key)
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
        async with lock:
            return await func(*args, **kwargs)

    return wrapped


async def review_controls(
    state: FSMContext, owner: int, scope: str, *, accept: bool = True
) -> InlineKeyboardMarkup:
    token = secrets.token_hex(8)
    await state.update_data(prize_review_token=token, prize_draft_owner=owner)
    buttons = []
    if accept:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="确认本档奖品" if scope == "new" else "确认入库",
                    callback_data=PrizeDraftCb(
                        scope=scope, owner=owner, token=token, action="accept"
                    ).pack(),
                )
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                text="全部删除并重新添加",
                callback_data=PrizeDraftCb(
                    scope=scope, owner=owner, token=token, action="reset"
                ).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def valid_action(state: FSMContext, uid: int, action: PrizeDraftCb) -> bool:
    data = await state.get_data()
    return (
        action.owner == uid == data.get("prize_draft_owner")
        and action.token == data.get("prize_review_token")
        and action.action in {"accept", "reset"}
    )
