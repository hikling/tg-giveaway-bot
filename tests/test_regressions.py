"""Regression cases for configuration, verification, concurrency and recovery.

No Telegram or Cloudflare requests: external services are represented by
fakes. These cases are intentionally not executed during the static repair.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.callbacks.factories import JoinCb
from bot.db import repo
from bot.db.models import GiveawayStatus
from bot.db.repo import (
    add_participant,
    count_participants,
    get_giveaway,
    list_winners,
    log_winners,
    pending_notifications,
    replace_tier_winners,
)
from bot.handlers import participate
from bot.main import _configure_delivery
from bot.prize_policy import CLAIM_WINDOW
from bot.services import giveaway as gw_service
from bot.services.channels import missing_channels, validate_channels
from bot.services.notifier import retry_pending, winner_delivery_lock
from bot.settings import Settings
from bot.utils.esc import telegram_text_length


@pytest.mark.parametrize(
    "value,expected",
    [
        ("424242", [424242]),
        ("424242, 424243", [424242, 424243]),
        ("[424242, 424243]", [424242, 424243]),
        ("", []),
    ],
)
def test_admin_ids_from_environment(monkeypatch, value, expected):
    monkeypatch.setenv("ADMIN_IDS", value)
    settings = Settings(_env_file=None)
    assert settings.admin_ids == expected


async def test_concurrent_draws_produce_one_result(sessionmaker, due_giveaway, bot):
    gid = await due_giveaway()
    async with sessionmaker() as session:
        await add_participant(session, giveaway_id=gid, user_id=1001, username="winner")
        await session.commit()
    await asyncio.gather(
        gw_service.finalize_and_publish(gid, bot),
        gw_service.finalize_and_publish(gid, bot),
    )
    async with sessionmaker() as session:
        assert len(await list_winners(session, gid)) == 1
    assert len([m for m in bot.sent if m[0] == 1001]) == 1


async def test_large_draw_sends_one_public_summary_and_each_winner_a_dm(
    sessionmaker, due_giveaway, bot
):
    tier_a = "奖" * 64
    tier_b = "二等奖"
    members = list(range(1001, 1261))
    gid = await due_giveaway(
        prize_tiers=[
            {"name": tier_a, "count": 180},
            {"name": tier_b, "count": 80},
        ]
    )
    async with sessionmaker() as session:
        for user_id in members:
            await add_participant(session, giveaway_id=gid, user_id=user_id, username=None)
        await session.commit()
    await gw_service.attach_message(gid, chat_id=-100123, message_id=7)

    await gw_service.finalize_and_publish(gid, bot)

    public_posts = [(message, args) for chat, message, args in bot.sent if chat == -100123]
    assert len(public_posts) == 1
    message, args = public_posts[0]
    assert telegram_text_length(message) < 512
    assert args["reply_to_message_id"] == 7
    assert "本轮中奖人数：260 人" in message
    assert f"/result {gid}" in message
    assert "tg://user?id=" not in message
    assert sorted(chat for chat, _, _ in bot.sent if chat > 0) == members


async def test_public_summary_keeps_seed_proof_without_winner_mentions(
    sessionmaker, due_giveaway, bot
):
    members = range(1001, 1071)
    gid = await due_giveaway(
        prize_tiers=[{"name": "一等奖", "count": len(members)}],
        draw_seed="secret-seed",
        draw_commit="seed-hash",
    )
    async with sessionmaker() as session:
        for user_id in members:
            await add_participant(session, giveaway_id=gid, user_id=user_id, username=None)
        await session.commit()
    await gw_service.attach_message(gid, chat_id=-100123, message_id=9)

    await gw_service.finalize_and_publish(gid, bot)

    public_posts = [(message, args) for chat, message, args in bot.sent if chat == -100123]
    assert len(public_posts) == 1
    message, args = public_posts[0]
    assert args["reply_to_message_id"] == 9
    assert telegram_text_length(message) <= 4096
    assert "<blockquote expandable>" in message and "</blockquote>" in message
    assert "secret-seed" in message
    assert "本轮中奖人数：70 人" in message
    assert "tg://user?id=" not in message


async def test_winner_dm_retries_after_telegram_flood_limit(
    sessionmaker, due_giveaway, bot, monkeypatch
):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    gid = await due_giveaway()
    async with sessionmaker() as session:
        await add_participant(session, giveaway_id=gid, user_id=1001, username="winner")
        await session.commit()

    attempts = 0
    original_send = bot.send_message

    async def flaky_send(chat_id, text, **kwargs):
        nonlocal attempts
        if chat_id == 1001 and attempts == 0:
            attempts += 1
            raise TelegramRetryAfter(
                method=SendMessage(chat_id=chat_id, text=text),
                message="Flood control exceeded",
                retry_after=0,
            )
        await original_send(chat_id, text, **kwargs)

    monkeypatch.setattr(bot, "send_message", flaky_send)
    await gw_service.finalize_and_publish(gid, bot)

    assert attempts == 1
    assert len([m for m in bot.sent if m[0] == 1001]) == 1
    async with sessionmaker() as session:
        assert not await pending_notifications(session, gid)


async def test_failed_draw_rolls_back_its_status_claim(
    sessionmaker,
    due_giveaway,
    bot,
    monkeypatch,
):
    gid = await due_giveaway()
    monkeypatch.setattr(
        gw_service, "log_winners", AsyncMock(side_effect=RuntimeError("disk failure"))
    )
    with pytest.raises(RuntimeError):
        await gw_service.finalize_and_publish(gid, bot)
    async with sessionmaker() as session:
        assert (await get_giveaway(session, gid)).status == GiveawayStatus.running
        assert not await list_winners(session, gid)
    assert not bot.sent


@pytest.mark.parametrize("is_member,expected", [(False, ("@required",)), (True, ())])
async def test_restricted_users_must_still_be_members(is_member, expected):
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(
                status="restricted",
                is_member=is_member,
            )
        )
    )
    assert await missing_channels(bot, 1001, ["@required"]) == expected


async def test_required_channel_needs_bot_admin_permissions(bot):
    bot.set_member("@required", 777000, "member")
    assert await validate_channels(bot, ["@required"])
    bot.set_member("@required", 777000, "administrator")
    assert not await validate_channels(bot, ["@required"])


async def test_failed_winner_dm_can_be_retried(sessionmaker, due_giveaway, bot):
    gid = await due_giveaway(prize_mode="unique")
    async with sessionmaker() as session:
        await add_participant(session, giveaway_id=gid, user_id=1001, username="winner")
        await session.commit()
    bot.fail_for.add(1001)
    await gw_service.finalize_and_publish(gid, bot)
    async with sessionmaker() as session:
        assert len(await pending_notifications(session, gid)) == 1
    bot.fail_for.clear()
    assert await retry_pending(bot, gid) == 1
    assert "48 小时" in bot.sent[-1][1] and "领奖截止" in bot.sent[-1][1]
    assert await retry_pending(bot, gid) == 0
    async with sessionmaker() as session:
        assert not await pending_notifications(session, gid)


async def test_retry_skips_expired_winner_dm(sessionmaker, due_giveaway, bot):
    gid = await due_giveaway(prize_mode="unique")
    async with sessionmaker() as session:
        giveaway = await get_giveaway(session, gid)
        giveaway.status = GiveawayStatus.finished
        expired, current = await log_winners(session, gid, [(1001, "Steam card"), (1002, "Steam card")])
        expired.drawn_at -= CLAIM_WINDOW
        await session.commit()
        assert [row.id for row in await pending_notifications(session, gid)] == [current.id]
    assert await retry_pending(bot, gid) == 1
    assert [chat for chat, _, _ in bot.sent] == [1002]


async def test_retry_waits_for_initial_winner_dm(sessionmaker, due_giveaway, bot, monkeypatch):
    gid = await due_giveaway()
    async with sessionmaker() as session:
        await add_participant(session, giveaway_id=gid, user_id=1001, username="winner")
        await session.commit()

    send_started = asyncio.Event()
    finish_send = asyncio.Event()
    retry_waiting = asyncio.Event()
    send_message = bot.send_message
    delivery_lock = winner_delivery_lock(gid)
    acquire = delivery_lock.acquire

    async def blocked_send(chat_id, text, **kwargs):
        send_started.set()
        await finish_send.wait()
        await send_message(chat_id, text, **kwargs)

    async def observe_acquire():
        if delivery_lock.locked():
            retry_waiting.set()
        return await acquire()

    monkeypatch.setattr(bot, "send_message", blocked_send)
    monkeypatch.setattr(delivery_lock, "acquire", observe_acquire)

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        tasks.create_task(gw_service.finalize_and_publish(gid, bot))
        await send_started.wait()
        retry = tasks.create_task(retry_pending(bot, gid))
        await retry_waiting.wait()
        finish_send.set()

    assert retry.result() == 0
    assert [chat_id for chat_id, _, _ in bot.sent] == [1001]
    async with sessionmaker() as session:
        assert not await pending_notifications(session, gid)


async def test_retry_ignores_legacy_winner_rows_before_draw(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway(prize_mode="unique")
    async with sessionmaker() as session:
        await log_winners(session, gid, [(1001, "Steam card")])
        await session.commit()

    assert await retry_pending(bot, gid) == 0
    assert not bot.sent
    async with sessionmaker() as session:
        assert (await list_winners(session, gid))[0].notified_at is None


async def test_superseded_failed_winner_is_not_retried(sessionmaker, due_giveaway, bot):
    gid = await due_giveaway()
    async with sessionmaker() as session:
        await add_participant(session, giveaway_id=gid, user_id=1001, username="winner")
        await session.commit()
    bot.fail_for.add(1001)
    await gw_service.finalize_and_publish(gid, bot)
    async with sessionmaker() as session:
        await replace_tier_winners(session, gid, "Steam card")
        await session.commit()
    bot.fail_for.clear()
    assert await retry_pending(bot, gid) == 0


async def test_polling_clears_webhook_without_dropping_updates():
    bot = SimpleNamespace(delete_webhook=AsyncMock(), set_webhook=AsyncMock())
    settings = Settings(_env_file=None, admin_ids=[], webhook_base_url="")
    await _configure_delivery(bot, settings)
    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    bot.set_webhook.assert_not_awaited()


async def test_webhook_registration_logs_do_not_expose_secret(caplog):
    bot = SimpleNamespace(delete_webhook=AsyncMock(), set_webhook=AsyncMock())
    settings = Settings(
        _env_file=None,
        admin_ids=[],
        webhook_base_url="https://giveaway.example.com",
        webhook_secret="never-log-this-secret",
    )
    with caplog.at_level("INFO", logger="bot"):
        await _configure_delivery(bot, settings)
    bot.set_webhook.assert_awaited_once_with(
        url=settings.webhook_url,
        secret_token=settings.webhook_secret,
        drop_pending_updates=False,
    )
    assert settings.webhook_secret not in caplog.text
    assert settings.masked_webhook_url() in caplog.text


def _on_join():
    return next(
        h.callback
        for h in participate.build_participate_router().callback_query.handlers
        if h.callback.__name__ == "on_join"
    )


def _callback(bot):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=1001, username="joiner"),
        bot=bot,
        answer=AsyncMock(),
    )


async def test_locked_eligibility_rechecks_deadline_after_waiting_for_writer(
    sessionmaker, new_giveaway, monkeypatch
):
    now = datetime.now(UTC)
    gid = await new_giveaway(end_at=now - timedelta(seconds=1))
    # The conditional UPDATE captures the old time before the lock wait.
    monkeypatch.setattr(repo, "_now", lambda: now - timedelta(minutes=1))

    async with sessionmaker() as session:
        assert await participate._locked_eligibility(session, gid, 1001) == "join.ended"


async def test_cancel_during_channel_check_cannot_accept_an_entry(
    sessionmaker,
    new_giveaway,
    bot,
    monkeypatch,
):
    gid = await new_giveaway(required_channels=["@promo"])

    async def check_channels(*args, **kwargs):
        await gw_service.cancel(gid)
        return ()

    monkeypatch.setattr(participate, "missing_channels", check_channels)
    cq = _callback(bot)
    await _on_join()(cq, JoinCb(giveaway_id=gid))
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0
    assert cq.answer.await_args.kwargs["show_alert"] is True


async def test_concurrent_join_creates_one_entry(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    first, second = _callback(bot), _callback(bot)
    handler = _on_join()
    await asyncio.gather(
        handler(first, JoinCb(giveaway_id=gid)),
        handler(second, JoinCb(giveaway_id=gid)),
    )
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0
        from bot.db.models import PoetryAttempt

        assert (await session.get(PoetryAttempt, (gid, 1001))).wrong_count == 0
    assert sorted(
        [
            first.answer.await_args.kwargs["show_alert"],
            second.answer.await_args.kwargs["show_alert"],
        ]
    ) == [False, False]
