"""命名链接、多位置同一抽奖，以及25题升级兼容。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from bot.db import base
from bot.db.models import Giveaway, GiveawayPost, GiveawayStatus, PoetryAttempt, PoetryRotation
from bot.db.repo import count_participants
from bot.handlers.admin_create import GiveawayDraft, build_admin_create_router
from bot.services.channels import (
    display_requirement,
    linked_requirement,
    linked_requirements,
    missing_channels,
    parse_requirements,
    resolve_destinations,
)
from bot.services.giveaway import create, finalize_and_publish
from bot.services.publication import post_locations, publish_all
from bot.services.scheduler import GiveawayScheduler
from tests.conftest import FakeBot
from tests.test_poetry import begin, choose, stored


class PostingBot(FakeBot):
    async def get_chat(self, ref):
        ids = {"@news": -100111, "@alias": -100111, "@group": -100222}
        cid = ids.get(str(ref), int(ref) if str(ref).startswith("-") else -100333)
        return SimpleNamespace(
            id=cid, type="channel" if cid == -100111 else "supergroup", title="实际名称"
        )

    async def send_message(self, chat_id, text, **kwargs):
        await super().send_message(chat_id, text, **kwargs)
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=len(self.sent))


async def new_multi_game(*, destinations=None, **overrides):
    params = dict(
        creator_id=424242,
        title="同一场抽奖",
        description="",
        prize_tiers=[{"name": "奖品", "count": 1}],
        required_channels=["@news|https://t.me/news|官方频道"],
        required_groups=["-100222|https://t.me/+invite|交流群"],
        end_at=datetime.now(UTC) + timedelta(hours=1),
        turnstile_required=False,
        publishing=True,
        destinations=destinations
        if destinations is not None
        else [(-100111, "官方频道"), (-100222, "交流群")],
    )
    params.update(overrides)
    return await create(**params)


@pytest.mark.parametrize(
    "text,canonical",
    [
        ("官方频道 | https://t.me/news", "@news|https://t.me/news|官方频道"),
        ("交流群 | https://t.me/+invite | -100222", "-100222|https://t.me/+invite|交流群"),
        ("@news | https://t.me/news | 官方频道", "@news|https://t.me/news|官方频道"),
        ("News | https://t.me/news", "@news|https://t.me/news|News"),
        ("-100222 | https://t.me/+invite", "-100222|https://t.me/+invite"),
    ],
)
def test_named_invites_parse_and_round_trip(text, canonical):
    assert parse_requirements(text) == [canonical]
    assert parse_requirements(canonical) == [canonical]


@pytest.mark.parametrize(
    "text",
    [
        "群名 | https://t.me/+abc",
        "名称 | javascript:alert(1)",
        "名称 | https://evil.example/x | -100222",
        "名称 | https://t.me/news | invalid|extra",
        "名称 | https://t.me/+abc | -0",
        "名称 | https://t.me/news | ",
    ],
)
def test_invalid_link_bindings_are_rejected(text):
    with pytest.raises(ValueError):
        parse_requirements(text)


def test_only_the_name_is_visible_and_markup_is_escaped():
    raw = parse_requirements("频道<b>&测试 | https://t.me/news")[0]
    rendered = str(linked_requirement(raw))
    assert rendered == '<a href="https://t.me/news">频道&lt;b&gt;&amp;测试</a>'
    assert display_requirement(raw) == "频道<b>&测试"
    assert str(linked_requirements([raw])) == rendered


async def test_member_check_uses_bound_id_instead_of_invite_url():
    bot = PostingBot()
    bot.set_member("-100222", 1001, "member")
    raw = parse_requirements("交流群 | https://t.me/+secret | -100222")[0]
    assert await missing_channels(bot, 1001, [raw]) == ()
    assert await missing_channels(bot, 1002, [raw]) == (raw,)


async def test_aliases_deduplicate_by_real_chat_id():
    result = await resolve_destinations(
        PostingBot(), ["@news", "-100111|https://t.me/news|官方频道", "@alias"]
    )
    assert len(result) == 1 and result[0].chat_id == -100111


async def test_mismatched_public_link_and_id_are_rejected():
    with pytest.raises(ValueError):
        await resolve_destinations(PostingBot(), ["-100222|https://t.me/news|错误链接"])


def test_target_selection_step_is_removed():
    assert not hasattr(GiveawayDraft, "waiting_target")
    assert all(
        h.callback.__name__ != "step_target" for h in build_admin_create_router().message.handlers
    )


async def test_one_giveaway_is_posted_to_every_destination_with_named_links(sessionmaker):
    bot = PostingBot()
    gw = await new_multi_game()
    result = await publish_all(bot, gw.id)
    assert result.sent == 2 and not result.failed
    assert {chat for chat, _, _ in bot.sent} == {-100111, -100222}
    assert (
        len({kw["reply_markup"].inline_keyboard[0][0].callback_data for _, _, kw in bot.sent}) == 1
    )
    for _, text, _ in bot.sent:
        assert '<a href="https://t.me/news">官方频道</a>' in text
        assert '<a href="https://t.me/+invite">交流群</a>' in text
        assert "（https://" not in text and "|https://" not in text
    async with sessionmaker() as session:
        posts = (await session.execute(select(GiveawayPost))).scalars().all()
        assert len(posts) == 2 and all(p.message_id for p in posts)
        assert not (await session.get(Giveaway, gw.id)).publishing
    await publish_all(bot, gw.id)
    assert len(bot.sent) == 2


async def test_all_posts_share_entry_and_draw_results_go_to_both_locations(sessionmaker):
    bot = PostingBot()
    gw = await new_multi_game()
    await publish_all(bot, gw.id)
    bot.set_member("@news", 1001, "member")
    bot.set_member("-100222", 1001, "member")
    question, _ = await begin(bot, gw.id)
    await choose(bot, question, correct=True)
    await begin(bot, gw.id)  # 从另一处抽奖帖点击，仍只有一条报名。
    async with sessionmaker() as session:
        assert await count_participants(session, gw.id) == 1
        game = await session.get(Giveaway, gw.id)
        game.end_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    await finalize_and_publish(gw.id, bot)
    notices = [(chat, kw) for chat, text, kw in bot.sent if "中奖名单" in text]
    assert {chat for chat, _ in notices} == {-100111, -100222}
    assert all(kw.get("reply_to_message_id") for _, kw in notices)
    assert len([text for chat, text, _ in bot.sent if chat == 1001 and "恭喜" in text]) == 1


async def test_partial_failure_can_retry_without_duplicate_posts(sessionmaker):
    bot = PostingBot()
    gw = await new_multi_game()
    bot.fail_for.add(-100222)
    result = await publish_all(bot, gw.id)
    assert result.sent == 1 and result.failed == ("交流群",) and not result.canceled
    async with sessionmaker() as session:
        assert (await session.get(Giveaway, gw.id)).status == GiveawayStatus.running
    bot.fail_for.clear()
    result = await publish_all(bot, gw.id)
    assert result.sent == 2 and not result.failed
    assert [chat for chat, _, _ in bot.sent].count(-100111) == 1
    assert [chat for chat, _, _ in bot.sent].count(-100222) == 1


async def test_all_failed_posts_cancel_the_giveaway(sessionmaker):
    bot = PostingBot()
    gw = await new_multi_game()
    bot.fail_for.update({-100111, -100222})
    result = await publish_all(bot, gw.id)
    assert result.canceled and result.sent == 0
    async with sessionmaker() as session:
        assert (await session.get(Giveaway, gw.id)).status == GiveawayStatus.canceled
    with pytest.raises(ValueError):
        await publish_all(bot, gw.id)


async def test_initial_posting_prevents_early_entry_or_early_draw(sessionmaker):
    bot = PostingBot()
    gw = await new_multi_game()
    _, query = await begin(bot, gw.id)
    assert "正在发布" in query.answer.await_args.args[0]
    async with sessionmaker() as session:
        game = await session.get(Giveaway, gw.id)
        game.end_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    await finalize_and_publish(gw.id, bot)
    async with sessionmaker() as session:
        assert (await session.get(Giveaway, gw.id)).status == GiveawayStatus.running
    assert not bot.sent


async def test_interrupted_publication_resumes_on_restart_and_never_reposts_recorded_sites(
    sessionmaker,
):
    bot = PostingBot()
    gw = await new_multi_game()
    async with sessionmaker() as session:
        row = await session.get(GiveawayPost, (gw.id, -100111))
        row.message_id = 77
        await session.commit()
    url = base.get_engine().url.render_as_string(hide_password=False)
    await base.get_engine().dispose()
    await base.init_db(url)
    scheduler = GiveawayScheduler(bot)
    await scheduler.restore_from_db()
    assert [chat for chat, _, _ in bot.sent] == [-100222]
    locations = await post_locations(gw.id)
    assert (-100111, 77) in locations and len(locations) == 2


async def test_concurrent_republish_does_not_duplicate_messages(sessionmaker):
    bot = PostingBot()
    gw = await new_multi_game()
    await asyncio.gather(publish_all(bot, gw.id), publish_all(bot, gw.id))
    assert len(bot.sent) == 2


async def test_lottery_deadline_during_publication_is_finalized_after_send(sessionmaker):
    class SlowBot(PostingBot):
        async def send_message(self, chat_id, text, **kwargs):
            result = await super().send_message(chat_id, text, **kwargs)
            if "🏆" in text:
                async with base.get_sessionmaker()() as session:
                    game = await session.get(Giveaway, gw.id)
                    game.end_at = datetime.now(UTC) - timedelta(seconds=1)
                    await session.commit()
            return result

    bot = SlowBot()
    gw = await new_multi_game()
    await publish_all(bot, gw.id)
    async with sessionmaker() as session:
        assert (await session.get(Giveaway, gw.id)).status == GiveawayStatus.finished
    assert any("已结束" in text for _, text, _ in bot.sent)


async def test_retired_question_is_replaced_without_deducting_a_chance(
    sessionmaker, new_giveaway, bot
):
    gid = await new_giveaway()
    original, _ = await begin(bot, gid)
    async with sessionmaker() as session:
        row = await session.get(PoetryAttempt, (gid, 1001))
        row.question_id = 49
        row.wrong_count = 2
        await session.commit()
    query = await choose(bot, original, correct=True)
    assert "不扣机会" in query.answer.await_args.args[0]
    row = await stored(sessionmaker, gid)
    assert 0 <= row.question_id < 25 and row.wrong_count == 2
    assert row.nonce != original.nonce
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0


async def test_old_rotation_ids_are_pruned_and_retired_pending_question_resends_safely(
    sessionmaker, new_giveaway, bot
):
    gid = await new_giveaway()
    await begin(bot, gid)
    async with sessionmaker() as session:
        row = await session.get(PoetryAttempt, (gid, 1001))
        row.question_id = 40
        rotation = await session.get(PoetryRotation, 1001)
        rotation.used_ids = list(range(50))
        await session.commit()
    new, _ = await begin(bot, gid)
    assert 0 <= new.question_id < 25 and new.remaining == 5
    async with sessionmaker() as session:
        rotation = await session.get(PoetryRotation, 1001)
        assert len(rotation.used_ids) == 1 and rotation.used_ids[0] < 25


async def test_republish_does_not_cancel_legacy_giveaways_without_publication_rows(
    sessionmaker, new_giveaway
):
    gid = await new_giveaway()
    with pytest.raises(ValueError):
        await publish_all(PostingBot(), gid)
    async with sessionmaker() as session:
        game = await session.get(Giveaway, gid)
        assert game.status == GiveawayStatus.running and not game.publishing


async def test_old_single_post_fallback_and_database_upgrade_are_preserved(
    sessionmaker, new_giveaway
):
    import sqlite3

    from bot.services.giveaway import attach_message

    gid = await new_giveaway()
    await attach_message(gid, -100111, 77)
    engine = base.get_engine()
    url = engine.url.render_as_string(hide_password=False)
    path = engine.url.database
    await engine.dispose()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE giveaway_posts")
        conn.execute("ALTER TABLE giveaways DROP COLUMN publishing")
    await base.init_db(url)
    assert await post_locations(gid, -100111, 77) == [(-100111, 77)]
    async with base.get_sessionmaker()() as session:
        assert not (await session.get(Giveaway, gid)).publishing
        assert not (await session.execute(select(GiveawayPost))).scalars().all()


async def test_expired_retired_question_counts_once_instead_of_getting_free_time(
    sessionmaker, new_giveaway, bot
):
    gid = await new_giveaway()
    original, _ = await begin(bot, gid)
    async with sessionmaker() as session:
        row = await session.get(PoetryAttempt, (gid, 1001))
        row.question_id = 49
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    await choose(bot, original, correct=True)
    row = await stored(sessionmaker, gid)
    assert row.wrong_count == 1 and row.nonce is None


async def test_creator_cannot_skip_all_destinations(sessionmaker):
    from tests.test_wizard_prize import FakeMessage, _ctx, _handler

    state = _ctx()
    await state.update_data(channels=[])
    await state.set_state(GiveawayDraft.waiting_groups)
    message = FakeMessage("-")
    await _handler("step_groups")(message, state, PostingBot())
    assert await state.get_state() == GiveawayDraft.waiting_groups.state
    assert "至少" in message.answers[-1]
