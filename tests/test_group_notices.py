from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import DeleteMessage, SendMessage
from aiogram.types import Chat, Message, Update, User

from bot.db.models import Giveaway, GiveawayPost, GiveawayStatus
from bot.db.repo import add_admin, log_winners
from bot.filters import admin as admin_filter
from bot.handlers import build_router
from bot.services import prize as prize_service
from bot.services.scheduler import GiveawayScheduler

_USER = User(id=1001, is_bot=False, first_name="Alice")
_GROUP = Chat(id=-1001234, type=ChatType.SUPERGROUP)


async def _feed_message(bot: Bot, scheduler: GiveawayScheduler, chat: Chat, text: str) -> None:
    dispatcher = Dispatcher(scheduler=scheduler)
    dispatcher.include_router(build_router())
    incoming = Message(message_id=8, date=datetime.now(UTC), chat=chat, from_user=_USER, text=text)
    await dispatcher.feed_update(bot, Update(update_id=1, message=incoming))


@pytest.mark.parametrize(
    ("chat_type", "command", "notice"),
    [
        (ChatType.GROUP, "/start", "欢迎使用"),
        (ChatType.SUPERGROUP, "/start", "欢迎使用"),
        (ChatType.SUPERGROUP, "/help", "命令列表"),
        (ChatType.SUPERGROUP, "/lang", "仅支持中文"),
        (ChatType.SUPERGROUP, "/me", "参与过"),
        (ChatType.SUPERGROUP, "/claim", "用法"),
        (ChatType.SUPERGROUP, "/myprizes", "私聊"),
        (ChatType.SUPERGROUP, "/list", "没有进行中的抽奖"),
        (ChatType.SUPERGROUP, "/prizestatus", "用法"),
        (ChatType.PRIVATE, "/start", "欢迎使用"),
        (ChatType.PRIVATE, "/help", "命令列表"),
    ],
)
async def test_command_and_reply_each_expire_after_ten_seconds_in_groups(
    sessionmaker, chat_type, command, notice
):
    if command in {"/list", "/prizestatus"}:
        async with sessionmaker() as session:
            await add_admin(session, _USER.id)
            await session.commit()

    chat_id = _USER.id if chat_type == ChatType.PRIVATE else _GROUP.id
    chat = Chat(id=chat_id, type=chat_type)
    reply = Message(message_id=9, date=datetime.now(UTC), chat=chat)

    async with Bot("777000:fake-token") as bot:
        requests = AsyncMock(side_effect=[reply, True, True])
        bot.session.make_request = requests
        scheduler = GiveawayScheduler(bot)

        before = datetime.now(UTC) + timedelta(seconds=10)
        await _feed_message(bot, scheduler, chat, command)
        after = datetime.now(UTC) + timedelta(seconds=10)

        requests.assert_awaited_once()
        sent = requests.await_args.args[1]
        assert isinstance(sent, SendMessage)
        assert sent.chat_id == chat_id
        assert notice in sent.text

        jobs = scheduler._scheduler.get_jobs()
        if chat_type == ChatType.PRIVATE:
            assert jobs == []
            return

        assert len(jobs) == 2
        for job in jobs:
            assert before <= job.trigger.run_date <= after
            await job.func(**job.kwargs)
        assert [call.args[1] for call in requests.await_args_list[1:]] == [
            DeleteMessage(chat_id=chat_id, message_id=8),
            DeleteMessage(chat_id=chat_id, message_id=9),
        ]


@pytest.mark.parametrize("text", ["普通聊天", "/unknown", "/help@another_bot", "/list"])
async def test_group_ignores_messages_that_match_no_handler(sessionmaker, monkeypatch, text):
    monkeypatch.setattr(admin_filter, "get_settings", lambda: SimpleNamespace(admin_ids=[]))
    async with Bot("777000:fake-token") as bot:
        bot.me = AsyncMock(
            return_value=User(id=bot.id, is_bot=True, first_name="Test", username="test_bot")
        )
        bot.session.make_request = AsyncMock()
        scheduler = GiveawayScheduler(bot)

        await _feed_message(bot, scheduler, _GROUP, text)

        assert scheduler._scheduler.get_jobs() == []
        bot.session.make_request.assert_not_awaited()


async def test_group_claim_only_deletes_command_when_prize_is_sent_privately(
    sessionmaker, due_giveaway
):
    gid = await due_giveaway(prize_mode="unique")
    async with sessionmaker() as session:
        giveaway = await session.get(Giveaway, gid)
        giveaway.status = GiveawayStatus.finished
        await log_winners(session, gid, [(_USER.id, "Steam card")])
        await session.commit()
    await prize_service.add_codes(gid, "Steam card", ["WINNER-CODE"])
    private = Chat(id=_USER.id, type=ChatType.PRIVATE)

    async with Bot("777000:fake-token") as bot:
        requests = AsyncMock(
            return_value=Message(message_id=9, date=datetime.now(UTC), chat=private)
        )
        bot.session.make_request = requests
        scheduler = GiveawayScheduler(bot)

        await _feed_message(bot, scheduler, _GROUP, f"/claim {gid}")

        requests.assert_awaited_once()
        sent = requests.await_args.args[1]
        assert isinstance(sent, SendMessage)
        assert sent.chat_id == private.id and "WINNER-CODE" in sent.text
        (job,) = scheduler._scheduler.get_jobs()
        assert job.kwargs == {"chat_id": _GROUP.id, "message_id": 8}


async def test_group_reply_is_deleted_even_when_bot_cannot_delete_commands(sessionmaker):
    command_delete = DeleteMessage(chat_id=_GROUP.id, message_id=8)
    reply = Message(message_id=9, date=datetime.now(UTC), chat=_GROUP)

    async with Bot("777000:fake-token") as bot:
        requests = AsyncMock(
            side_effect=[
                reply,
                TelegramBadRequest(method=command_delete, message="message can't be deleted"),
                True,
            ]
        )
        bot.session.make_request = requests
        scheduler = GiveawayScheduler(bot)

        await _feed_message(bot, scheduler, _GROUP, "/help")
        for job in scheduler._scheduler.get_jobs():
            await job.func(**job.kwargs)

        assert [call.args[1] for call in requests.await_args_list[1:]] == [
            command_delete,
            DeleteMessage(chat_id=_GROUP.id, message_id=9),
        ]


async def test_group_republish_keeps_the_giveaway_post(sessionmaker, new_giveaway):
    gid = await new_giveaway()
    async with sessionmaker() as session:
        await add_admin(session, _USER.id)
        session.add(GiveawayPost(giveaway_id=gid, chat_id=_GROUP.id, label="测试群"))
        await session.commit()
    now = datetime.now(UTC)
    post = Message(message_id=9, date=now, chat=_GROUP)
    reply = Message(message_id=10, date=now, chat=_GROUP)

    async with Bot("777000:fake-token") as bot:
        requests = AsyncMock(side_effect=[post, reply])
        bot.session.make_request = requests
        scheduler = GiveawayScheduler(bot)

        await _feed_message(bot, scheduler, _GROUP, f"/republish {gid}")

        assert requests.await_count == 2
        assert requests.await_args_list[0].args[1].reply_markup is not None
        assert [job.kwargs["message_id"] for job in scheduler._scheduler.get_jobs()] == [8, 10]


async def test_scheduled_notice_delete_handles_throttling_and_already_deleted_messages():
    method = DeleteMessage(chat_id=-1001234, message_id=9)
    bot = SimpleNamespace(
        delete_message=AsyncMock(
            side_effect=[
                TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=0),
                TelegramBadRequest(method=method, message="message to delete not found"),
            ]
        ),
    )
    scheduler = GiveawayScheduler(bot)
    await asyncio.wait_for(
        scheduler._delete_group_message(method.chat_id, method.message_id), timeout=1
    )
    assert [call.kwargs for call in bot.delete_message.await_args_list] == [
        {"chat_id": method.chat_id, "message_id": method.message_id},
        {"chat_id": method.chat_id, "message_id": method.message_id},
    ]
