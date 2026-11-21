"""实际报名处理器的数据库、并发、轮换、隔离与重启回归。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.callbacks.factories import JoinCb, PoetryCb
from bot.db import base
from bot.db.models import PoetryAttempt, PoetryRotation
from bot.db.repo import ban_user, count_participants, set_user_lang
from bot.handlers.participate import build_participate_router, question_message
from bot.poetry import POEMS
from bot.services import giveaway as giveaways
from bot.services import poetry


def handler(name):
    return next(
        h.callback
        for h in build_participate_router().callback_query.handlers
        if h.callback.__name__ == name
    )


def cq(bot, uid=1001):
    return SimpleNamespace(
        bot=bot,
        from_user=SimpleNamespace(id=uid, username="测试用户"),
        answer=AsyncMock(),
        message=None,
    )


async def begin(bot, gid, uid=1001):
    query = cq(bot, uid)
    await handler("on_join")(query, JoinCb(giveaway_id=gid))
    async with base.get_sessionmaker()() as session:
        row = await session.get(PoetryAttempt, (gid, uid))
        return poetry.view(row) if row else None, query


async def choose(bot, challenge, *, correct, uid=1001):
    answer = POEMS[challenge.question_id].answer
    index = next(i for i, value in enumerate(challenge.options) if (value == answer) == correct)
    query = cq(bot, uid)
    await handler("on_poetry")(
        query, PoetryCb(giveaway_id=challenge.giveaway_id, nonce=challenge.nonce, choice=index)
    )
    return query


async def stored(sessionmaker, gid, uid=1001):
    async with sessionmaker() as session:
        return await session.get(PoetryAttempt, (gid, uid))


def test_bank_has_fifty_unambiguous_two_character_questions():
    assert len(POEMS) == 50
    assert len({p.prompt for p in POEMS}) == 50
    for p in POEMS:
        assert p.prompt.count("□□") == 1
        assert len(p.answer) == 2
        assert all(len(x) == 2 for x in p.distractors)
        assert len({p.answer, *p.distractors}) == 4
        assert p.title and p.author


async def test_correct_answer_enters_without_another_join(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    challenge, _ = await begin(bot, gid)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0
    query = await choose(bot, challenge, correct=True)
    assert "报名成功" in query.answer.await_args.args[0]
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 1
    assert (await stored(sessionmaker, gid)).nonce is None


async def test_five_errors_lock_only_this_giveaway(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    question_ids = set()
    for i in range(5):
        challenge, _ = await begin(bot, gid)
        question_ids.add(challenge.question_id)
        query = await choose(bot, challenge, correct=False)
        assert (await stored(sessionmaker, gid)).wrong_count == i + 1
    assert len(question_ids) == 5
    assert "五次" in query.answer.await_args.args[0]
    _, query = await begin(bot, gid)
    assert "五次" in query.answer.await_args.args[0]
    await choose(bot, challenge, correct=True)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0
    other = await new_giveaway()
    fresh, _ = await begin(bot, other)
    assert fresh.remaining == 5
    await choose(bot, fresh, correct=True)
    async with sessionmaker() as session:
        assert await count_participants(session, other) == 1


async def test_four_errors_then_correct_is_allowed(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    for _ in range(4):
        challenge, _ = await begin(bot, gid)
        await choose(bot, challenge, correct=False)
    last, _ = await begin(bot, gid)
    assert last.remaining == 1
    await choose(bot, last, correct=True)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 1


async def test_replay_and_reopen_do_not_change_question_or_spend_twice(
    sessionmaker, new_giveaway, bot
):
    gid = await new_giveaway()
    first, _ = await begin(bot, gid)
    same, _ = await begin(bot, gid)
    assert first == same
    await choose(bot, first, correct=False)
    await choose(bot, first, correct=True)
    current = await stored(sessionmaker, gid)
    assert current.wrong_count == 1 and current.nonce != first.nonce
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0


async def test_nonce_is_bound_to_user_and_giveaway(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    first, _ = await begin(bot, gid)
    other, _ = await begin(bot, gid, 1002)
    await choose(bot, first, correct=True, uid=1002)
    assert (await stored(sessionmaker, gid, 1002)).nonce == other.nonce
    gid2 = await new_giveaway()
    own, _ = await begin(bot, gid2)
    forged = poetry.Challenge(gid2, first.question_id, first.options, first.nonce, 5)
    await choose(bot, forged, correct=True)
    assert (await stored(sessionmaker, gid2)).nonce == own.nonce
    await choose(bot, first, correct=True)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 1
        assert await count_participants(session, gid2) == 0


@pytest.mark.parametrize("correct", [True, False])
async def test_concurrent_answers_consume_one_question(sessionmaker, new_giveaway, bot, correct):
    gid = await new_giveaway()
    challenge, _ = await begin(bot, gid)
    await asyncio.gather(*(choose(bot, challenge, correct=correct) for _ in range(4)))
    row = await stored(sessionmaker, gid)
    assert row.wrong_count == (0 if correct else 1)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == (1 if correct else 0)


async def test_pending_and_lock_survive_new_database_engine(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    for _ in range(4):
        challenge, _ = await begin(bot, gid)
        await choose(bot, challenge, correct=False)
    pending, _ = await begin(bot, gid)
    engine = base.get_engine()
    url = engine.url.render_as_string(hide_password=False)
    await engine.dispose()
    await base.init_db(url)
    restored, _ = await begin(bot, gid)
    assert restored == pending and restored.remaining == 1
    await choose(bot, restored, correct=False)
    await base.get_engine().dispose()
    await base.init_db(url)
    _, query = await begin(bot, gid)
    assert "五次" in query.answer.await_args.args[0]


async def test_fifty_questions_rotate_across_giveaways_without_repeating(
    sessionmaker, new_giveaway, bot
):
    ids = []
    last = None
    for _ in range(51):
        gid = await new_giveaway()
        last, _ = await begin(bot, gid)
        ids.append(last.question_id)
    assert len(set(ids[:50])) == 50
    assert ids[49] != ids[50]
    async with sessionmaker() as session:
        rotation = await session.get(PoetryRotation, 1001)
        assert rotation.used_ids == [ids[50]]


async def test_channel_membership_is_rechecked_when_answering(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway(required_channels=["@promo"])
    bot.set_member("@promo", 1001, "member")
    challenge, _ = await begin(bot, gid)
    bot.members.clear()
    await choose(bot, challenge, correct=True)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0
    assert (await stored(sessionmaker, gid)).wrong_count == 0
    bot.set_member("@promo", 1001, "member")
    await choose(bot, challenge, correct=True)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 1


@pytest.mark.parametrize("state", ["canceled", "expired", "banned"])
async def test_closed_or_banned_cannot_answer(sessionmaker, new_giveaway, bot, state):
    gid = await new_giveaway()
    challenge, _ = await begin(bot, gid)
    if state == "canceled":
        await giveaways.cancel(gid)
    else:
        async with sessionmaker() as session:
            if state == "expired":
                from bot.db.models import Giveaway

                gw = await session.get(Giveaway, gid)
                gw.end_at = datetime.now(UTC) - timedelta(seconds=1)
            else:
                await ban_user(session, 1001)
            await session.commit()
    await choose(bot, challenge, correct=True)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0


async def test_failed_dm_preserves_question_and_chances(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    bot.fail_for.add(1001)
    challenge, query = await begin(bot, gid)
    assert "/start" in query.answer.await_args.args[0]
    bot.fail_for.clear()
    resent, _ = await begin(bot, gid)
    assert resent == challenge and resent.remaining == 5


async def test_old_english_preferences_and_legacy_captcha_do_not_bypass_poetry(
    sessionmaker, new_giveaway, bot
):
    gid = await new_giveaway(turnstile_required=True)
    challenge, _ = await begin(bot, gid)
    async with sessionmaker() as session:
        await set_user_lang(session, 1001, "en")
        await session.commit()
    same, _ = await begin(bot, gid)
    assert same == challenge
    assert "请选择" in bot.sent[-1][1]
    await choose(bot, challenge, correct=True)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 1


async def test_correct_answer_and_entry_rollback_together(
    sessionmaker, new_giveaway, bot, monkeypatch
):
    gid = await new_giveaway()
    challenge, _ = await begin(bot, gid)
    original = poetry.add_participant

    async def fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("模拟写入故障")

    monkeypatch.setattr(poetry, "add_participant", fail)
    with pytest.raises(RuntimeError):
        await choose(bot, challenge, correct=True)
    assert (await stored(sessionmaker, gid)).nonce == challenge.nonce
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0


async def test_callbacks_do_not_expose_answer_and_fit_telegram_limit(
    sessionmaker, new_giveaway, bot
):
    gid = await new_giveaway()
    challenge, _ = await begin(bot, gid)
    _, keyboard = question_message(challenge)
    for button in sum(keyboard.inline_keyboard, []):
        assert len(button.callback_data.encode()) <= 64
        assert POEMS[challenge.question_id].answer not in button.callback_data
    assert len(PoetryCb(giveaway_id=2**63 - 1, nonce="a" * 24, choice=3).pack().encode()) <= 64


async def test_invalid_choice_does_not_spend_a_chance(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    challenge, _ = await begin(bot, gid)
    for index in [-1, 4, 999999]:
        await handler("on_poetry")(
            cq(bot), PoetryCb(giveaway_id=gid, nonce=challenge.nonce, choice=index)
        )
    row = await stored(sessionmaker, gid)
    assert row.wrong_count == 0 and row.nonce == challenge.nonce


async def test_cancellation_during_answer_channel_check_is_rejected(
    sessionmaker, new_giveaway, bot, monkeypatch
):
    from bot.handlers import participate

    gid = await new_giveaway(required_channels=["@promo"])
    bot.set_member("@promo", 1001, "member")
    challenge, _ = await begin(bot, gid)

    async def cancel_then_return(*args):
        await giveaways.cancel(gid)
        return ()

    monkeypatch.setattr(participate, "missing_channels", cancel_then_return)
    await choose(bot, challenge, correct=True)
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0
    assert (await stored(sessionmaker, gid)).nonce == challenge.nonce


async def test_wrong_answer_updates_private_message(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    challenge, _ = await begin(bot, gid)
    query = cq(bot)
    query.message = SimpleNamespace(chat=SimpleNamespace(id=1001), edit_text=AsyncMock())
    wrong = next(
        i for i, text in enumerate(challenge.options) if text != POEMS[challenge.question_id].answer
    )
    await handler("on_poetry")(
        query, PoetryCb(giveaway_id=gid, nonce=challenge.nonce, choice=wrong)
    )
    assert "回答错误" in query.message.edit_text.await_args.args[0]
    assert "剩余答题机会：4" in bot.sent[-1][1]


async def test_wizard_automatically_enforces_poetry_without_optional_captcha_step(sessionmaker):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot.handlers.admin_create import GiveawayDraft, build_admin_create_router
    from bot.settings import Settings
    from bot.utils.render import render

    router = build_admin_create_router()
    step = next(
        h.callback for h in router.message.handlers if h.callback.__name__ == "step_duration"
    )
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    message = SimpleNamespace(text="1h", from_user=SimpleNamespace(id=1), answer=AsyncMock())
    await step(message, state)
    assert await state.get_state() == GiveawayDraft.waiting_prizemode.state
    assert not hasattr(GiveawayDraft, "waiting_turnstile")
    assert Settings(_env_file=None, default_lang="en").default_lang == "zh"
    post = render(
        "giveaway_running",
        "en",
        id=1,
        title="抽奖",
        description="",
        prizes=[],
        channels=[],
        ends_at="今晚",
        count=0,
        commit="abc",
    )
    assert "古诗词" in post and "Cloudflare" not in post
    assert "Giveaway" not in post


async def test_no_optional_captcha_flag_can_disable_poetry(sessionmaker, new_giveaway, bot):
    for legacy_flag in (False, True):
        gid = await new_giveaway(turnstile_required=legacy_flag)
        challenge, _ = await begin(bot, gid)
        assert challenge is not None
        async with sessionmaker() as session:
            assert await count_participants(session, gid) == 0


async def test_concurrent_last_chance_cannot_bypass_lock(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    for _ in range(4):
        question, _ = await begin(bot, gid)
        await choose(bot, question, correct=False)
    last, _ = await begin(bot, gid)
    await asyncio.gather(choose(bot, last, correct=False), choose(bot, last, correct=True))
    row = await stored(sessionmaker, gid)
    async with sessionmaker() as session:
        count = await count_participants(session, gid)
    assert (row.wrong_count, count) in [(4, 1), (5, 0)]


async def test_non_ascii_nonce_is_rejected_without_using_a_chance(sessionmaker, new_giveaway, bot):
    gid = await new_giveaway()
    challenge, _ = await begin(bot, gid)
    await handler("on_poetry")(cq(bot), PoetryCb(giveaway_id=gid, nonce="错误凭据", choice=0))
    row = await stored(sessionmaker, gid)
    assert row.wrong_count == 0 and row.nonce == challenge.nonce


async def test_unverified_entries_never_reach_the_draw_pool(sessionmaker, new_giveaway):
    from bot.db.repo import add_participant, list_participants

    gid = await new_giveaway()
    async with sessionmaker() as session:
        await add_participant(session, giveaway_id=gid, user_id=1, username="合格")
        await add_participant(
            session, giveaway_id=gid, user_id=2, username="未验证", verified=False
        )
        await session.commit()
        pool = await list_participants(session, gid, verified_only=True)
    assert [p.user_id for p in pool] == [1]
