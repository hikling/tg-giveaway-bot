"""频道与群组同时校验、满人开奖和持久化60秒时限。"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.callbacks.factories import PoetryCb
from bot.db import base
from bot.db.models import Giveaway, GiveawayStatus, PoetryAttempt
from bot.db.repo import add_participant, count_participants, list_winners
from bot.poetry import POEMS
from bot.services import giveaway as giveaways
from bot.services import poetry
from bot.services.channels import missing_channels, parse_requirements, validate_channels
from bot.services.scheduler import GiveawayScheduler
from bot.utils.timeparse import COUNT_ONLY_END, format_draw_condition, parse_draw_condition
from tests.test_poetry import begin, choose, cq, handler, stored


async def make_expired(sessionmaker, gid, uid=1001, *, wrong_count=None):
    async with sessionmaker() as session:
        row = await session.get(PoetryAttempt, (gid, uid))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        if wrong_count is not None:
            row.wrong_count = wrong_count
        await session.commit()


async def test_both_channel_and_group_are_required(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway(
        required_channels=["@news"], required_groups=["-100123|https://t.me/+invite"]
    )
    bot.set_member("@news", 1001, "member")
    challenge, query = await begin(bot, gid)
    assert challenge is None
    assert "-100123" in query.answer.await_args.args[0]
    bot.set_member("-100123", 1001, "member")
    challenge, _ = await begin(bot, gid)
    assert challenge is not None
    # 群组退出后，即便答对也不能报名。
    bot.set_member("-100123", 1001, "left")
    await choose(bot, challenge, correct=True)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0
    bot.set_member("-100123", 1001, "member")
    await choose(bot, challenge, correct=True)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 1


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@news\nhttps://t.me/discussion", ["@news", "@discussion"]),
        ("-100123 | https://t.me/+invite", ["-100123|https://t.me/+invite"]),
        ("-123456", ["-123456"]),
        ("t.me/example, @news", ["@example", "@news"]),
        ("@news\n@news", ["@news"]),
        ("-", []),
    ],
)
def test_chat_reference_formats(raw, expected):
    assert parse_requirements(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://t.me/+invite",
        "https://t.me/joinchat/secret",
        "-100|https://evil.example/",
        "@",
        "0",
        "123456",
        "-100|",
    ],
)
def test_uncheckable_or_invalid_references_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_requirements(raw)


async def test_numeric_group_id_is_sent_as_integer_and_invalid_refs_fail_closed():
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")))
    assert await missing_channels(bot, 1001, ["-100123"]) == ()
    bot.get_chat_member.assert_awaited_once_with(-100123, 1001)
    assert await missing_channels(bot, 1001, ["https://t.me/+secret"])


async def test_creation_checks_chat_type_and_admin_privileges():
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=7)),
        get_chat=AsyncMock(return_value=SimpleNamespace(type="supergroup")),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator")),
    )
    assert not await validate_channels(bot, ["-100123"], expected_type="group")
    assert await validate_channels(bot, ["-100123"], expected_type="channel")
    bot.get_chat_member.return_value.status = "member"
    assert await validate_channels(bot, ["-100123"], expected_type="group")


@pytest.mark.parametrize(
    "text,mode,target",
    [
        ("满100人", "count", 100),
        ("100人", "count", 100),
        ("满100人参加开奖", "count", 100),
        ("1h", "time", None),
        ("1h或满100人", "either", 100),
        ("满100人或者1h", "either", 100),
    ],
)
def test_draw_condition_modes(text, mode, target):
    parsed = parse_draw_condition(text, "Asia/Shanghai")
    assert parsed is not None and parsed[0] == mode and parsed[2] == target
    assert parsed[1].tzinfo is not None
    label = format_draw_condition(parsed[1], mode, target, "Asia/Shanghai")
    assert "9999" not in label


@pytest.mark.parametrize(
    "text",
    [
        "满0人",
        "满-1人",
        "满1.5人",
        "满999999999999999999人",
        "x",
        "1h或2h",
        "满1人或满2人",
        "1h或满1人或满2人",
    ],
)
def test_invalid_draw_conditions(text):
    assert parse_draw_condition(text) is None


async def test_full_count_draws_only_after_successful_answers(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway(draw_mode="count", target_participants=2, end_at=COUNT_ONLY_END)
    first, _ = await begin(bot, gid, 1001)
    second, _ = await begin(bot, gid, 1002)
    # 仅点击参加、看到题目不计报名人数。
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0
    await choose(bot, first, correct=True, uid=1001)
    async with sessionmaker() as session:
        assert (await session.get(Giveaway, gid)).status == GiveawayStatus.running
        assert not await list_winners(session, gid)
    await choose(bot, second, correct=False, uid=1002)
    async with sessionmaker() as session:
        assert (await session.get(Giveaway, gid)).status == GiveawayStatus.running
    second, _ = await begin(bot, gid, 1002)
    await choose(bot, second, correct=True, uid=1002)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 2
        assert (await session.get(Giveaway, gid)).status == GiveawayStatus.finished
        assert len(await list_winners(session, gid)) == 1


async def test_concurrent_answers_do_not_overfill_or_draw_twice(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway(draw_mode="count", target_participants=2, end_at=COUNT_ONLY_END)
    challenges = [await begin(bot, gid, uid) for uid in range(1001, 1005)]
    await asyncio.gather(
        *(
            choose(bot, entry[0], correct=True, uid=uid)
            for uid, entry in zip(range(1001, 1005), challenges)
        )
    )
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 2
        assert len(await list_winners(session, gid)) == 1
    assert len([text for _, text, _ in bot.sent if "恭喜" in text]) == 1


async def test_count_draw_failure_rolls_back_last_entry(
    sessionmaker, new_giveaway, bot, monkeypatch
):
    gid = await new_giveaway(draw_mode="count", target_participants=1, end_at=COUNT_ONLY_END)
    challenge, _ = await begin(bot, gid)
    monkeypatch.setattr(giveaways, "log_winners", AsyncMock(side_effect=RuntimeError("磁盘故障")))
    with pytest.raises(RuntimeError):
        await choose(bot, challenge, correct=True)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0
        assert (await session.get(Giveaway, gid)).status == GiveawayStatus.running
        assert (await session.get(PoetryAttempt, (gid, 1001))).nonce == challenge.nonce


async def test_count_draw_publishes_even_if_callback_reply_fails(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway(draw_mode="count", target_participants=1, end_at=COUNT_ONLY_END)
    challenge, _ = await begin(bot, gid)
    query = cq(bot)
    query.answer.side_effect = RuntimeError("回调已过期")
    index = challenge.options.index(POEMS[challenge.question_id].answer)
    with pytest.raises(RuntimeError):
        await handler("on_poetry")(
            query, PoetryCb(giveaway_id=gid, nonce=challenge.nonce, choice=index)
        )
    assert any("恭喜" in text for _, text, _ in bot.sent)


async def test_count_only_has_no_timer_and_restores_full_games(sessionmaker, new_giveaway, bot):
    full = await new_giveaway(draw_mode="count", target_participants=1, end_at=COUNT_ONLY_END)
    empty = await new_giveaway(draw_mode="count", target_participants=2, end_at=COUNT_ONLY_END)
    async with sessionmaker() as session:
        await add_participant(session, giveaway_id=full, user_id=1001, username="a")
        await session.commit()
    scheduler = GiveawayScheduler(bot)
    await scheduler.restore_from_db()
    assert not scheduler._scheduler.get_jobs()
    async with sessionmaker() as session:
        assert (await session.get(Giveaway, full)).status == GiveawayStatus.finished
        assert (await session.get(Giveaway, empty)).status == GiveawayStatus.running
    await scheduler.restore_from_db()
    assert len([text for _, text, _ in bot.sent if "恭喜" in text]) == 1


@pytest.mark.parametrize("count_first", [True, False])
async def test_either_mode_draws_once_when_first_condition_is_reached(
    sessionmaker, new_giveaway, bot, count_first
):
    gid = await new_giveaway(draw_mode="either", target_participants=1)
    challenge, _ = await begin(bot, gid)
    if count_first:
        await choose(bot, challenge, correct=True)
        await giveaways.finalize_and_publish(gid, bot)
    else:
        await giveaways.finalize_and_publish(gid, bot)
        await choose(bot, challenge, correct=True)
    async with sessionmaker() as session:
        assert (await session.get(Giveaway, gid)).status == GiveawayStatus.finished
        assert await count_participants(session, gid) == (1 if count_first else 0)


async def test_deadline_is_sixty_seconds_and_repeated_join_does_not_extend_it(
    sessionmaker, new_giveaway, bot
):
    gid = await new_giveaway()
    before = datetime.now(UTC)
    challenge, _ = await begin(bot, gid)
    assert (
        before + timedelta(seconds=59)
        < challenge.expires_at
        <= datetime.now(UTC) + timedelta(seconds=60)
    )
    same, _ = await begin(bot, gid)
    assert same.expires_at == challenge.expires_at and same.nonce == challenge.nonce


async def test_timeout_is_automatically_one_error_without_clicking(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    challenge, _ = await begin(bot, gid)
    await make_expired(sessionmaker, gid)
    assert await poetry.expire_due_questions(bot) == 1
    row = await stored(sessionmaker, gid)
    assert row.wrong_count == 1 and row.nonce is None
    assert "超时" in bot.sent[-1][1] and "4" in bot.sent[-1][1]
    assert await poetry.expire_due_questions(bot) == 0
    # 离开不操作不会不停生成题目和连续扣机会。
    assert (await stored(sessionmaker, gid)).wrong_count == 1
    new, _ = await begin(bot, gid)
    assert new.nonce != challenge.nonce and new.remaining == 4
    assert new.question_id != challenge.question_id


async def test_late_correct_answer_is_an_error_before_worker_runs(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    challenge, _ = await begin(bot, gid)
    await make_expired(sessionmaker, gid)
    query = await choose(bot, challenge, correct=True)
    assert "60秒" in query.answer.await_args.args[0]
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0
    assert (await stored(sessionmaker, gid)).wrong_count == 1
    await choose(bot, challenge, correct=False)
    assert (await stored(sessionmaker, gid)).wrong_count == 1


async def test_timeout_and_answer_race_only_counts_once(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    challenge, _ = await begin(bot, gid)
    await make_expired(sessionmaker, gid)
    await asyncio.gather(
        poetry.expire_due_questions(bot),
        choose(bot, challenge, correct=True),
        poetry.expire_due_questions(bot),
    )
    assert (await stored(sessionmaker, gid)).wrong_count == 1
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0


async def test_timeout_on_fifth_chance_locks_this_game(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    challenge, _ = await begin(bot, gid)
    await make_expired(sessionmaker, gid, wrong_count=4)
    assert await poetry.expire_due_questions(bot) == 1
    assert (await stored(sessionmaker, gid)).wrong_count == 5
    _, query = await begin(bot, gid)
    assert "五次" in query.answer.await_args.args[0]
    other = await new_giveaway()
    fresh, _ = await begin(bot, other)
    assert fresh.remaining == 5
    await choose(bot, challenge, correct=True)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0


async def test_expired_question_is_counted_on_restart(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    await begin(bot, gid)
    await make_expired(sessionmaker, gid)
    url = base.get_engine().url.render_as_string(hide_password=False)
    await base.get_engine().dispose()
    await base.init_db(url)
    scheduler = GiveawayScheduler(bot)
    await scheduler.restore_from_db()
    async with base.get_sessionmaker()() as session:
        row = await session.get(PoetryAttempt, (gid, 1001))
        assert row.wrong_count == 1 and row.nonce is None
    await poetry.expire_due_questions(bot)
    async with base.get_sessionmaker()() as session:
        assert (await session.get(PoetryAttempt, (gid, 1001))).wrong_count == 1


async def test_automatic_scheduler_job_counts_timeout(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    await begin(bot, gid)
    await make_expired(sessionmaker, gid)
    scheduler = GiveawayScheduler(bot)
    scheduler.start()
    try:
        for _ in range(20):
            await asyncio.sleep(0.1)
            if (await stored(sessionmaker, gid)).wrong_count == 1:
                break
        assert (await stored(sessionmaker, gid)).wrong_count == 1
    finally:
        scheduler.shutdown()
        await asyncio.sleep(0)


async def test_timeout_notification_failure_does_not_restore_chance(
    sessionmaker, new_giveaway, bot
):
    gid = await new_giveaway()
    await begin(bot, gid)
    await make_expired(sessionmaker, gid)
    bot.fail_for.add(1001)
    assert await poetry.expire_due_questions(bot) == 1
    assert (await stored(sessionmaker, gid)).wrong_count == 1


async def test_failed_initial_delivery_does_not_spend_opportunities(
    sessionmaker, new_giveaway, bot
):
    gid = await new_giveaway()
    bot.fail_for.add(1001)
    challenge, _ = await begin(bot, gid)
    assert challenge.expires_at is None
    assert await poetry.expire_due_questions(bot) == 0
    assert (await stored(sessionmaker, gid)).wrong_count == 0


async def test_upgrade_preserves_games_and_questions_and_adds_timeout_columns(
    sessionmaker, new_giveaway, bot
):
    gid = await new_giveaway()
    original, _ = await begin(bot, gid)
    engine = base.get_engine()
    url = engine.url.render_as_string(hide_password=False)
    path = engine.url.database
    await engine.dispose()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX ix_poetry_attempts_expires_at")
        for column in ("expires_at", "sent_at"):
            conn.execute(f"ALTER TABLE poetry_attempts DROP COLUMN {column}")
        for column in ("required_groups", "draw_mode", "target_participants"):
            conn.execute(f"ALTER TABLE giveaways DROP COLUMN {column}")
    await base.init_db(url)
    sm = base.get_sessionmaker()
    async with sm() as session:
        gw = await session.get(Giveaway, gid)
        question = await session.get(PoetryAttempt, (gid, 1001))
        assert (
            gw.draw_mode == "time" and gw.required_groups == [] and gw.target_participants is None
        )
        assert question.nonce == original.nonce and question.expires_at is not None
        deadline = question.expires_at
    # 再次启动不会重新补60秒。
    await base.get_engine().dispose()
    await base.init_db(url)
    async with base.get_sessionmaker()() as session:
        assert (await session.get(PoetryAttempt, (gid, 1001))).expires_at == deadline


async def test_many_missing_memberships_do_not_overflow_telegram_alert(
    sessionmaker, new_giveaway, bot
):
    channels = ["@" + "a" * 20 + str(i) for i in range(10)]
    groups = ["-100123|https://t.me/+" + "b" * 80]
    gid = await new_giveaway(required_channels=channels, required_groups=groups)
    challenge, query = await begin(bot, gid)
    assert challenge is None
    assert len(query.answer.await_args.args[0]) <= 200
    assert all(name in bot.sent[-1][1] for name in channels)
    assert "群组" in bot.sent[-1][1]


async def test_creator_sets_channel_group_and_count_condition_then_publishes(sessionmaker):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot.db.repo import list_running_giveaways
    from bot.handlers.admin_create import GiveawayDraft, build_admin_create_router
    from tests.conftest import FakeBot

    class WizardBot(FakeBot):
        async def get_chat(self, ref):
            return SimpleNamespace(
                id=-100111 if ref == "@news" else -100222,
                type="channel" if ref == "@news" else "supergroup",
                title="发布频道",
            )

        async def send_message(self, chat_id, text, **kwargs):
            await super().send_message(chat_id, text, **kwargs)
            return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=1)

    bot = WizardBot()
    bot.set_member("@news", 777000, "administrator")
    bot.set_member("-100222", 777000, "administrator")
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=777000, chat_id=424242, user_id=424242)
    )
    handlers = {
        h.callback.__name__: h.callback for h in build_admin_create_router().message.handlers
    }

    def message(text):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=424242), text=text, answer=AsyncMock(), forward_origin=None
        )

    await state.update_data(title="中文抽奖", description="", prizes=[{"name": "奖品", "count": 1}])
    await handlers["step_channels"](message("@news"), state, bot)
    assert await state.get_state() == GiveawayDraft.waiting_groups.state
    await handlers["step_groups"](message("-100222 | https://t.me/+invite"), state, bot)
    assert await state.get_state() == GiveawayDraft.waiting_duration.state
    await handlers["step_duration"](message("满100人"), state)
    data = await state.get_data()
    assert data["draw_mode"] == "count" and data["target_participants"] == 100
    await handlers["step_prizemode"](message("0"), state)
    confirm = message("@news")
    await handlers["step_target"](confirm, state, bot)
    summary = confirm.answer.await_args.args[0]
    assert "必加群组" in summary and "满 100 人" in summary and "9999" not in summary
    scheduler = SimpleNamespace(schedule=Mock())
    await handlers["step_confirm"](message("确认"), state, bot, scheduler)
    async with sessionmaker() as session:
        games = await list_running_giveaways(session)
        assert len(games) == 1
        assert games[0].required_channels == ["@news"]
        assert games[0].required_groups == ["-100222|https://t.me/+invite"]
        assert games[0].draw_mode == "count" and games[0].target_participants == 100
    post = bot.sent[-1][1]
    assert "必加群组" in post and "满 100 人" in post and "60秒" in post
    assert "9999" not in post and "invite" in post
    assert await state.get_state() is None


async def test_group_step_refuses_channel_in_place_of_group(sessionmaker):
    from bot.handlers.admin_create import GiveawayDraft
    from tests.test_wizard_prize import FakeMessage, _ctx, _handler

    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=7)),
        get_chat=AsyncMock(return_value=SimpleNamespace(type="channel")),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator")),
    )
    state = _ctx()
    await state.set_state(GiveawayDraft.waiting_groups)
    message = FakeMessage("@news")
    await _handler("step_groups")(message, state, bot)
    assert await state.get_state() == GiveawayDraft.waiting_groups.state
    assert "类型不符" in message.answers[-1]


async def test_blacklisted_and_unverified_entries_do_not_fill_count(
    sessionmaker, new_giveaway, bot
):
    from bot.db.repo import ban_user

    gid = await new_giveaway(draw_mode="count", target_participants=2, end_at=COUNT_ONLY_END)
    async with sessionmaker() as session:
        await add_participant(
            session, giveaway_id=gid, user_id=2001, username="未验证", verified=False
        )
        await add_participant(session, giveaway_id=gid, user_id=2002, username="黑名单")
        await ban_user(session, 2002)
        await session.commit()
    one, _ = await begin(bot, gid)
    await choose(bot, one, correct=True)
    async with sessionmaker() as session:
        assert (await session.get(Giveaway, gid)).status == GiveawayStatus.running
    two, _ = await begin(bot, gid, 1002)
    await choose(bot, two, correct=True, uid=1002)
    async with sessionmaker() as session:
        assert (await session.get(Giveaway, gid)).status == GiveawayStatus.finished
        assert {w.user_id for w in await list_winners(session, gid)} <= {1001, 1002}


async def test_reopening_expired_question_counts_before_sending_new_one(
    sessionmaker, new_giveaway, bot
):
    gid = await new_giveaway()
    original, _ = await begin(bot, gid)
    await make_expired(sessionmaker, gid)
    new, _ = await begin(bot, gid)
    assert new.remaining == 4 and new.nonce != original.nonce
    await choose(bot, original, correct=False)
    assert (await stored(sessionmaker, gid)).wrong_count == 1


def test_exact_sixty_second_boundary_is_expired():
    now = datetime.now(UTC)
    row = PoetryAttempt(
        giveaway_id=1,
        user_id=1,
        wrong_count=0,
        question_id=0,
        options=[],
        nonce="token",
        expires_at=now,
    )
    assert not poetry.expire_question(row, now - timedelta(microseconds=1))
    assert poetry.expire_question(row, now)
    assert row.wrong_count == 1 and row.nonce is None
    assert not poetry.expire_question(row, now + timedelta(seconds=60))


async def test_timeout_worker_skips_finished_games(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    await begin(bot, gid)
    await make_expired(sessionmaker, gid)
    await giveaways.cancel(gid)
    assert await poetry.expire_due_questions(bot) == 0
    assert (await stored(sessionmaker, gid)).wrong_count == 0


async def test_ambiguous_network_failure_keeps_timeout_active(sessionmaker, new_giveaway, bot):
    from aiogram.exceptions import TelegramNetworkError

    gid = await new_giveaway()
    bot.send_message = AsyncMock(
        side_effect=TelegramNetworkError("sendMessage", "连接中断，是否送达未知")
    )
    challenge, _ = await begin(bot, gid)
    # 请求可能已被Telegram接收，不能暂停计时而让已送达题目永不过期。
    assert challenge.expires_at is not None
    await make_expired(sessionmaker, gid)
    assert await poetry.expire_due_questions(bot) == 1
    assert (await stored(sessionmaker, gid)).wrong_count == 1


async def test_failed_resend_never_pauses_original_deadline(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    challenge, _ = await begin(bot, gid)
    bot.fail_for.add(1001)
    same, _ = await begin(bot, gid)
    assert same.expires_at == challenge.expires_at
    await make_expired(sessionmaker, gid)
    await poetry.expire_due_questions(bot)
    assert (await stored(sessionmaker, gid)).wrong_count == 1
