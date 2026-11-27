"""The draw itself: tier rules, reroll rules, and the audit trail.

Everything here used to be wrong in ways users could detect: one person could
win two tiers, a tier could be rerolled into itself, a running giveaway
could be rerolled (publishing winners early, then drawing again at the
timer), a blacklisted account kept its seat in the pool, and a tier that
could not be filled shrank in silence.
"""

from __future__ import annotations

import pytest

from bot.db.models import GiveawayStatus
from bot.db.prize_repo import CLAIM_EXPIRED, CLAIM_OK
from bot.db.repo import (
    add_participant,
    ban_user,
    current_winner_ids,
    get_giveaway,
    list_winners,
    replace_tier_winners,
)
from bot.prize_policy import CLAIM_WINDOW
from bot.services import giveaway as gw_service
from bot.services import prize as prize_service
from bot.services.giveaway import RerollError, finalize_and_publish, reroll_tier
from tests.conftest import ADMIN_ID


async def _join(session, giveaway_id, user_ids):
    for uid in user_ids:
        await add_participant(
            session, giveaway_id=giveaway_id, user_id=uid, username="u")


async def _status(sessionmaker, gid):
    async with sessionmaker() as session:
        gw = await get_giveaway(session, gid)
        return gw.status


@pytest.mark.parametrize("draw_mode", ["time", "either"])
async def test_timer_before_deadline_does_not_draw(sessionmaker, new_giveaway, bot, draw_mode):
    gid = await new_giveaway(
        draw_mode=draw_mode, target_participants=2 if draw_mode == "either" else None
    )
    await gw_service.attach_message(gid, chat_id=-100123, message_id=42)
    async with sessionmaker() as session:
        await _join(session, gid, [1001])
        await session.commit()

    await finalize_and_publish(gid, bot)

    assert await _status(sessionmaker, gid) == GiveawayStatus.running
    async with sessionmaker() as session:
        assert not await list_winners(session, gid)
    assert bot.sent == []


async def test_timer_cannot_close_a_count_only_giveaway(sessionmaker, due_giveaway, bot):
    gid = await due_giveaway(draw_mode="count", target_participants=2)
    async with sessionmaker() as session:
        await _join(session, gid, [1001])
        await session.commit()

    await finalize_and_publish(gid, bot)

    assert await _status(sessionmaker, gid) == GiveawayStatus.running
    async with sessionmaker() as session:
        assert not await list_winners(session, gid)
    assert bot.sent == []


async def test_draw_awards_the_tier_and_finishes(sessionmaker, due_giveaway, bot):
    gid = await due_giveaway(prize_tiers=[{"name": "Steam card", "count": 1}])
    async with sessionmaker() as session:
        await _join(session, gid, [1001, 1002, 1003])
        await session.commit()

    await finalize_and_publish(gid, bot)

    assert await _status(sessionmaker, gid) == GiveawayStatus.finished
    async with sessionmaker() as session:
        winners = await list_winners(session, gid)
    assert len(winners) == 1
    assert winners[0].tier_name == "Steam card"
    assert winners[0].replaced_at is None


async def test_draw_publishes_under_the_post_and_dms_the_winner(
    sessionmaker, due_giveaway, bot
):
    gid = await due_giveaway(prize_tiers=[{"name": "Key", "count": 1}], prize_mode="unique")
    async with sessionmaker() as session:
        await _join(session, gid, [1001])
        await session.commit()
    await gw_service.attach_message(gid, chat_id=-100123, message_id=42)

    await finalize_and_publish(gid, bot)

    public = [m for m in bot.sent if m[0] == -100123]
    assert len(public) == 1
    assert public[0][2].get("reply_to_message_id") == 42
    assert "48 小时" in public[0][1] and "自动回收" in public[0][1]
    dms = [m for m in bot.sent if m[0] == 1001]
    assert len(dms) == 1, "the winner is told directly"
    assert "Key" in dms[0][1]
    assert "48 小时" in dms[0][1] and "自动回收" in dms[0][1]
    assert "领奖截止" in dms[0][1]


async def test_one_user_cannot_win_two_tiers(sessionmaker, due_giveaway, bot):
    """A cross-tier repeat used to be possible: there was no exclusion set."""
    gid = await due_giveaway(
        prize_tiers=[
            {"name": "Gold", "count": 2},
            {"name": "Silver", "count": 2},
        ]
    )
    async with sessionmaker() as session:
        await _join(session, gid, [1001, 1002, 1003])
        await session.commit()

    await finalize_and_publish(gid, bot)

    async with sessionmaker() as session:
        rows = await list_winners(session, gid)

    ids = [r.user_id for r in rows]
    assert len(ids) == len(set(ids)), f"repeat winner: {ids}"
    tiers = [r.tier_name for r in rows]
    assert tiers.count("Gold") == 2
    # Three people entered, so Silver takes one seat and the remaining
    # seat is reported as a shortfall instead of quietly dropped.
    assert tiers.count("Silver") == 1


async def test_shortfall_is_announced_in_public(sessionmaker, due_giveaway, bot):
    """P1-5: a shrinking tier used to be announced as fully awarded."""
    gid = await due_giveaway(prize_tiers=[{"name": "Rare", "count": 3}])
    async with sessionmaker() as session:
        await _join(session, gid, [1001])
        await session.commit()
    await gw_service.attach_message(gid, chat_id=-100123, message_id=7)

    await finalize_and_publish(gid, bot)

    public = [m for m in bot.sent if m[0] == -100123][0][1]
    assert "本轮中奖人数：1 人" in public
    assert "空缺 2 个名额" in public


async def test_blacklisted_participant_is_excluded_from_the_draw(
    sessionmaker, due_giveaway, bot
):
    """P1-3: a ban used to be checked only at join time."""
    gid = await due_giveaway(prize_tiers=[{"name": "Key", "count": 1}])
    async with sessionmaker() as session:
        await _join(session, gid, [1001, 1002])
        await session.commit()

    async with sessionmaker() as session:
        await ban_user(session, 1001, reason="abuse", banned_by=1)
        await session.commit()

    await finalize_and_publish(gid, bot)

    async with sessionmaker() as session:
        winners = await current_winner_ids(session, gid)
    assert winners == {1002}


async def test_draw_with_no_participants_still_closes_the_giveaway(
    sessionmaker, due_giveaway, bot
):
    gid = await due_giveaway()
    await gw_service.attach_message(gid, chat_id=-100123, message_id=7)

    await finalize_and_publish(gid, bot)

    public = [m for m in bot.sent if m[0] == -100123][0][1]
    assert public.strip() != "", "the post still gets a closing line"
    assert await _status(sessionmaker, gid) == GiveawayStatus.finished


async def test_drawing_twice_does_nothing(sessionmaker, due_giveaway, bot):
    """The timer and a manual call must not produce two draws."""
    gid = await due_giveaway(prize_tiers=[{"name": "Key", "count": 1}])
    async with sessionmaker() as session:
        await _join(session, gid, [1001, 1002])
        await session.commit()

    await finalize_and_publish(gid, bot)
    sent_before = len(bot.sent)
    await finalize_and_publish(gid, bot)

    assert len(bot.sent) == sent_before
    async with sessionmaker() as session:
        rows = await list_winners(session, gid)
    assert len(rows) == 1


async def test_reroll_requires_a_finished_giveaway(
    sessionmaker, new_giveaway, bot
):
    """A running giveaway used to publish winners early, then draw again."""
    gid = await new_giveaway(prize_tiers=[{"name": "Key", "count": 1}])
    async with sessionmaker() as session:
        await _join(session, gid, [1001, 1002])
        await session.commit()

    with pytest.raises(RerollError) as exc:
        await reroll_tier(gid, "Key", bot)
    assert str(exc.value) == "running"
    assert bot.sent == []


async def test_reroll_refuses_a_cancelled_giveaway(
    sessionmaker, new_giveaway, bot
):
    gid = await new_giveaway(prize_tiers=[{"name": "Key", "count": 1}])
    assert await gw_service.cancel(gid) is True

    with pytest.raises(RerollError) as exc:
        await reroll_tier(gid, "Key", bot)
    assert str(exc.value) == "canceled"


async def test_reroll_refuses_an_unknown_tier(sessionmaker, due_giveaway, bot):
    gid = await due_giveaway(prize_tiers=[{"name": "Key", "count": 1}])
    async with sessionmaker() as session:
        await _join(session, gid, [1001, 1002])
        await session.commit()
    await finalize_and_publish(gid, bot)

    with pytest.raises(RerollError) as exc:
        await reroll_tier(gid, "Platinum", bot)
    assert str(exc.value) == "no_such_tier"


async def test_reroll_refuses_a_claimed_tier(sessionmaker, due_giveaway, bot):
    gid = await due_giveaway(prize_tiers=[{"name": "Key", "count": 1}], prize_mode="unique")
    await prize_service.add_codes(gid, "Key", ["CODE-ONE", "CODE-TWO"])
    async with sessionmaker() as session:
        await _join(session, gid, [1001, 1002])
        await session.commit()
    await finalize_and_publish(gid, bot)
    async with sessionmaker() as session:
        winner = (await list_winners(session, gid))[0]
    assert (await prize_service.claim(gid, winner.user_id, "Key")).status == CLAIM_OK
    sent_before = len(bot.sent)

    with pytest.raises(RerollError, match="^claimed$"):
        await reroll_tier(gid, "Key", bot)

    async with sessionmaker() as session:
        assert await current_winner_ids(session, gid) == {winner.user_id}
        assert len(await list_winners(session, gid, include_replaced=True)) == 1
    stock = await prize_service.stock(gid)
    assert stock["Key"].remaining == 1
    assert len(bot.sent) == sent_before


async def test_reroll_replaces_and_keeps_the_old_draw(
    sessionmaker, due_giveaway, bot
):
    """P1-4: the audit trail is kept, the live result is replaced."""
    gid = await due_giveaway(prize_tiers=[{"name": "Key", "count": 1}], prize_mode="unique")
    async with sessionmaker() as session:
        await _join(session, gid, [1001, 1002, 1003])
        await session.commit()

    await finalize_and_publish(gid, bot)
    async with sessionmaker() as session:
        first = await current_winner_ids(session, gid)
        original = (await list_winners(session, gid))[0]
        original.drawn_at -= CLAIM_WINDOW
        await session.commit()
    await prize_service.add_codes(gid, "Key", ["FRESH-WINNER-CODE"])
    assert (await prize_service.claim(gid, next(iter(first)), "Key")).status == CLAIM_EXPIRED

    picks, shortfall = await reroll_tier(gid, "Key", bot)

    assert len(picks) == 1
    assert shortfall is None
    async with sessionmaker() as session:
        live = await list_winners(session, gid)
        history = await list_winners(session, gid, include_replaced=True)

    assert len(history) == 2, "the superseded draw is kept for audit"
    assert len(live) == 1, "only the replacement counts as current"
    assert live[0].replaced_at is None
    superseded = [w for w in history if w.replaced_at is not None]
    assert len(superseded) == 1, "the old draw is stamped, not deleted"
    assert {superseded[0].user_id} == first
    assert live[0].user_id in {1001, 1002, 1003}
    assert await prize_service.withdraw_recovered_codes(gid, ADMIN_ID) == []
    assert (await prize_service.claim(gid, live[0].user_id, "Key")).code == "FRESH-WINNER-CODE"


async def test_reroll_never_rewards_the_same_person_twice(
    sessionmaker, due_giveaway, bot
):
    """Rerolling tier A must not hand a live winner of tier B another win."""
    gid = await due_giveaway(
        prize_tiers=[
            {"name": "Gold", "count": 1},
            {"name": "Silver", "count": 1},
        ]
    )
    async with sessionmaker() as session:
        await _join(session, gid, [1001, 1002])
        await session.commit()

    await finalize_and_publish(gid, bot)
    async with sessionmaker() as session:
        before = await current_winner_ids(session, gid)
    assert len(before) == 2

    picks, shortfall = await reroll_tier(gid, "Gold", bot)

    # Both entrants already hold a live win, so nobody is eligible for Gold.
    assert picks == []
    assert shortfall is not None and shortfall.granted == 0

    async with sessionmaker() as session:
        after = await current_winner_ids(session, gid)
        live = await list_winners(session, gid)
    assert after <= before, "no new winner appears out of thin air"
    assert [w.tier_name for w in live] == ["Silver"], "Gold has no live winner now"
    assert after == {live[0].user_id}, "the untouched Silver winner is untouched"


async def test_reroll_reports_a_shortfall_when_the_pool_is_tiny(
    sessionmaker, due_giveaway, bot
):
    gid = await due_giveaway(
        prize_tiers=[{"name": "Gold", "count": 1}, {"name": "Silver", "count": 1}]
    )
    async with sessionmaker() as session:
        await _join(session, gid, [1001])
        await session.commit()

    await finalize_and_publish(gid, bot)
    picks, shortfall = await reroll_tier(gid, "Gold", bot)

    assert picks == []
    assert shortfall is not None
    assert shortfall.missing == 1


async def test_reroll_of_a_giveaway_with_no_entries_is_empty(
    sessionmaker, new_giveaway, bot
):
    gid = await new_giveaway(prize_tiers=[{"name": "Key", "count": 1}])
    async with sessionmaker() as session:
        gw = await get_giveaway(session, gid)
        gw.status = GiveawayStatus.finished
        await session.commit()

    picks, shortfall = await reroll_tier(gid, "Key", bot)
    assert picks == []
    assert shortfall is not None
    assert shortfall.granted == 0


async def test_replace_tier_winners_only_touches_one_tier(
    sessionmaker, new_giveaway
):
    from bot.db.repo import log_winners

    gid = await new_giveaway()
    async with sessionmaker() as session:
        await log_winners(session, gid, [(1001, "Gold"), (1002, "Silver")])
        await session.commit()

        await replace_tier_winners(session, gid, "Gold")
        await session.commit()

        live = await current_winner_ids(session, gid)
    assert live == {1002}


def test_draw_indices_is_statistically_uniform_and_unbiased():
    """Verify partial Fisher-Yates draw has uniform slot frequency and no position bias."""
    from bot.utils.antiabuse import draw_indices

    n, k, trials = 20, 5, 4000
    counts = [0] * n
    for trial in range(trials):
        picked = draw_indices(f"fairness-check-seed-{trial}", n, k)
        assert len(picked) == k
        assert len(set(picked)) == k
        assert all(0 <= idx < n for idx in picked)
        for idx in picked:
            counts[idx] += 1

    expected = trials * k / n  # 1000.0 per participant slot
    chi_square = sum((obs - expected) ** 2 / expected for obs in counts)
    # df=19; p < 0.0005 threshold is ~46.2 — deterministic seeds keep this stable.
    assert chi_square < 45.0, f"non-uniform draw distribution: chi2={chi_square:.2f}, counts={counts}"
    first_half = sum(counts[: n // 2])
    second_half = sum(counts[n // 2 :])
    assert abs(first_half - second_half) < 400


async def test_large_multi_tier_draw_300_winners_no_duplicates_and_reproducible(
    sessionmaker, due_giveaway, bot
):
    from bot.utils.antiabuse import draw_indices, make_commit, reveal_ok

    seed, commit = make_commit()
    tiers = [
        {"name": "一等奖", "count": 50},
        {"name": "二等奖", "count": 100},
        {"name": "三等奖", "count": 150},
    ]
    gid = await due_giveaway(prize_tiers=tiers, draw_seed=seed, draw_commit=commit)
    all_users = list(range(10_000, 10_505))
    banned_users = {10_000, 10_100, 10_200, 10_300, 10_400}
    async with sessionmaker() as session:
        await _join(session, gid, all_users)
        for uid in banned_users:
            await ban_user(session, uid, reason="abuse", banned_by=1)
        await session.commit()
    await gw_service.attach_message(gid, chat_id=-100123, message_id=88)

    await finalize_and_publish(gid, bot)

    async with sessionmaker() as session:
        gw = await get_giveaway(session, gid)
        live = await list_winners(session, gid)

    assert gw.status == GiveawayStatus.finished
    assert reveal_ok(gw.draw_seed, gw.draw_commit)
    assert len(live) == 300
    winner_ids = [w.user_id for w in live]
    assert len(set(winner_ids)) == 300
    assert banned_users.isdisjoint(winner_ids)
    assert [w.tier_name for w in live].count("一等奖") == 50
    assert [w.tier_name for w in live].count("二等奖") == 100
    assert [w.tier_name for w in live].count("三等奖") == 150

    # Independent commit-reveal replay must reproduce the exact 300 winners.
    eligible = sorted(uid for uid in all_users if uid not in banned_users)
    expected: list[tuple[int, str]] = []
    already: set[int] = set()
    for tier in tiers:
        pool = [uid for uid in eligible if uid not in already]
        picks = [pool[i] for i in draw_indices(f"{seed}|{tier['name']}", len(pool), tier["count"])]
        already.update(picks)
        expected.extend((uid, tier["name"]) for uid in picks)
    assert [(w.user_id, w.tier_name) for w in live] == expected

    public_posts = [text for chat, text, _ in bot.sent if chat == -100123]
    assert len(public_posts) == 1
    assert seed in public_posts[0]
    assert "本轮中奖人数：300 人" in public_posts[0]
    assert "tg://user?id=" not in public_posts[0]
