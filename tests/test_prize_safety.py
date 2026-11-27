from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.enums import ChatType
from aiogram.filters import CommandObject
from aiogram.types import BufferedInputFile
from sqlalchemy import select

from bot.callbacks.factories import ClaimCb
from bot.db import prize_repo
from bot.db.base import get_engine, init_db
from bot.db.models import Giveaway, GiveawayStatus, PrizeClaim, PrizeCode
from bot.db.prize_repo import CLAIM_ALREADY, CLAIM_EMPTY, CLAIM_EXPIRED, CLAIM_INELIGIBLE, CLAIM_OK
from bot.db.repo import add_participant, ban_user, list_winners, log_winners, replace_tier_winners
from bot.handlers.prize import build_prize_admin_router, build_prize_router
from bot.i18n import t
from bot.prize_policy import CLAIM_WINDOW, CLAIM_WINDOW_HOURS
from bot.services import giveaway as giveaway_service
from bot.services import prize as prize_service
from tests.conftest import ADMIN_ID

WINNER = 1001
OTHER_USER = 1002
TIER = "Steam card"
OTHER_TIER = "Book"


def _handler(name, *, event="message"):
    observer = getattr(build_prize_router(), event)
    return next(
        handler.callback for handler in observer.handlers if handler.callback.__name__ == name
    )


def _message(uid, *, group=False):
    chat = SimpleNamespace(
        id=-1001002003004 if group else uid,
        type=ChatType.SUPERGROUP if group else ChatType.PRIVATE,
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=uid),
        chat=chat,
        answer=AsyncMock(return_value=SimpleNamespace(chat=chat, message_id=25)),
    )


async def _record_winners(
    sessionmaker, gid, winners, *, status=GiveawayStatus.finished, drawn_at=None
):
    async with sessionmaker() as session:
        gw = await session.get(Giveaway, gid)
        gw.status = status
        rows = await log_winners(session, gid, winners)
        if drawn_at is not None:
            for row in rows:
                row.drawn_at = drawn_at
        await session.commit()


@pytest.mark.parametrize("mode", ["unique", "shared"])
async def test_claim_expires_at_48_hours_and_keeps_claim_history(
    sessionmaker, new_giveaway, monkeypatch, mode
):
    gid = await new_giveaway(prize_mode=mode)
    drawn_at = datetime.now(UTC) - CLAIM_WINDOW
    await _record_winners(sessionmaker, gid, [(WINNER, TIER), (OTHER_USER, TIER)], drawn_at=drawn_at)
    now = drawn_at + CLAIM_WINDOW - timedelta(microseconds=1)
    monkeypatch.setattr(prize_repo, "datetime", SimpleNamespace(now=lambda _tz: now))
    await prize_service.add_codes(
        gid, TIER, ["for-first-winner", "spare"] if mode == "unique" else ["shared-content"],
        max_claims=prize_service.max_claims_for(mode),
    )

    received = await prize_service.claim(gid, WINNER, TIER)
    assert received.status == CLAIM_OK
    now += timedelta(microseconds=1)
    assert (await prize_service.claim(gid, OTHER_USER, TIER)).status == CLAIM_EXPIRED
    assert (await prize_service.claim(gid, WINNER, TIER)).status == CLAIM_ALREADY
    assert [prize.code for prize in await prize_service.claimed_by(WINNER)] == [received.code]
    assert await prize_service.claimed_by(OTHER_USER) == []


async def test_expired_claim_entrypoints_and_organizer_recovery(
    bot, sessionmaker, new_giveaway, monkeypatch
):
    gid = await new_giveaway(prize_mode="unique")
    drawn_at = datetime.now(UTC) - CLAIM_WINDOW - timedelta(minutes=1)
    await _record_winners(sessionmaker, gid, [(WINNER, TIER), (OTHER_USER, TIER)], drawn_at=drawn_at)
    await prize_service.add_codes(gid, TIER, ["issued-code", "recovered-code"])
    async with sessionmaker() as session:
        # A legacy duplicate must never appear twice in the recovered file.
        session.add(PrizeCode(giveaway_id=gid, tier_name=TIER, code="recovered-code"))
        await session.commit()

    with monkeypatch.context() as patch:
        patch.setattr(
            prize_repo, "datetime",
            SimpleNamespace(now=lambda _tz: drawn_at + timedelta(hours=1)),
        )
        assert (await prize_service.claim(gid, WINNER, TIER)).code == "issued-code"
        assert await prize_service.withdraw_recovered_codes(gid, ADMIN_ID) == []

    scheduler = Mock()
    callback = SimpleNamespace(from_user=SimpleNamespace(id=OTHER_USER), answer=AsyncMock())
    await _handler("on_claim_button", event="callback_query")(
        callback, ClaimCb(giveaway_id=gid, tier_index=0), bot
    )
    expired = t("prize.expired", hours=CLAIM_WINDOW_HOURS)
    callback.answer.assert_awaited_once_with(expired, show_alert=True)

    claimant = _message(OTHER_USER)
    await _handler("on_claim_command")(
        claimant, CommandObject(prefix="/", command="claim", args=str(gid)), bot, scheduler
    )
    claimant.answer.assert_awaited_once_with(expired)
    result = _message(OTHER_USER)
    await _handler("on_result")(
        result, CommandObject(prefix="/", command="result", args=str(gid)), scheduler
    )
    assert "逾期已回收" in result.answer.await_args.args[0]
    assert f"/claim {gid}" not in result.answer.await_args.args[0]
    assert not bot.sent

    stock = (await prize_service.stock(gid))[TIER]
    assert (stock.claimed, stock.recoverable, stock.withdrawn) == (1, 1, 0)
    admin_router = build_prize_admin_router()
    withdraw = next(
        h.callback for h in admin_router.message.handlers if h.callback.__name__ == "on_reclaim"
    )
    command = CommandObject(prefix="/", command="reclaim", args=str(gid))
    bot.send_document = AsyncMock()
    await withdraw(_message(ADMIN_ID, group=True), command, bot, scheduler)
    await withdraw(_message(OTHER_USER), command, bot, scheduler)
    bot.send_document.assert_not_awaited()

    host = _message(ADMIN_ID)
    bot.send_document.side_effect = [TimeoutError(), None]
    await withdraw(host, command, bot, scheduler)
    host.answer.assert_awaited_once_with(t("prize.reclaim_retry", id=gid))
    engine = get_engine()
    await engine.dispose()
    await init_db(engine.url.render_as_string())
    assert (await prize_service.claim(gid, OTHER_USER, TIER)).status == CLAIM_EXPIRED
    await withdraw(host, command, bot, scheduler)
    assert bot.send_document.await_count == 2
    for delivery in bot.send_document.await_args_list:
        assert delivery.args[0] == ADMIN_ID
        assert delivery.args[1].data == b"recovered-code\n"
    stock = (await prize_service.stock(gid))[TIER]
    assert (stock.claimed, stock.remaining, stock.recoverable, stock.withdrawn) == (1, 0, 0, 1)
    assert [prize.code for prize in await prize_service.claimed_by(WINNER)] == ["issued-code"]


async def test_result_shows_only_the_callers_current_win(sessionmaker, new_giveaway):
    gid = await new_giveaway(prize_mode="unique")
    result = _handler("on_result")
    command = CommandObject(prefix="/", command="result", args=str(gid))
    scheduler = Mock()
    pending = _message(WINNER)
    await result(pending, command, scheduler)
    assert "尚未开奖" in pending.answer.await_args.args[0]

    await _record_winners(sessionmaker, gid, [(OTHER_USER, TIER)])
    async with sessionmaker() as session:
        await replace_tier_winners(session, gid, TIER)
        await log_winners(session, gid, [(WINNER, TIER)])
        await session.commit()
    old_winner = _message(OTHER_USER)
    await result(old_winner, command, scheduler)
    assert "未查询到" in old_winner.answer.await_args.args[0]

    current_winner = _message(WINNER)
    await result(current_winner, command, scheduler)
    body = current_winner.answer.await_args.args[0]
    assert TIER in body and f"/claim {gid}" in body

    group = _message(WINNER, group=True)
    await result(group, command, scheduler)
    group.answer.assert_awaited_once_with(t("common.in_private_only"))


async def test_prize_history_stays_in_the_winners_private_chat(bot, sessionmaker, due_giveaway):
    gid = await due_giveaway(prize_mode="unique")
    await _record_winners(sessionmaker, gid, [(WINNER, TIER)])
    secret = "WINNER-PRIVATE-CODE"
    assert await prize_service.add_codes(gid, TIER, [secret]) == 1
    assert (await prize_service.claim(gid, WINNER, TIER)).status == CLAIM_OK

    history = _handler("on_myprizes")
    scheduler = Mock()
    group = _message(WINNER, group=True)
    await history(group, bot, scheduler)
    group.answer.assert_awaited_once_with(t("prize.private_history"))
    assert bot.sent == []

    unrelated = _message(OTHER_USER)
    await history(unrelated, bot, scheduler)
    unrelated.answer.assert_awaited_once_with(t("prize.myprizes_empty"))
    assert bot.sent == []

    private = _message(WINNER)
    await history(private, bot, scheduler)
    private.answer.assert_not_called()
    scheduler.schedule_group_message_deletion.assert_called_once_with(group.chat.id, 25)
    assert len(bot.sent) == 1
    recipient, body, _ = bot.sent[0]
    assert recipient == WINNER
    assert secret in body


@pytest.mark.parametrize("status", [GiveawayStatus.running, GiveawayStatus.canceled])
async def test_undrawn_results_cannot_release_prizes(bot, sessionmaker, new_giveaway, status):
    gid = await new_giveaway(prize_mode="unique")
    secret = "NOT-READY-TO-RELEASE"
    await prize_service.add_codes(gid, TIER, [secret])
    # A database upgraded from a version that allowed an early reroll can
    # already have winner rows for an unfinished or canceled giveaway.
    await _record_winners(sessionmaker, gid, [(WINNER, TIER)], status=status)

    callback = SimpleNamespace(from_user=SimpleNamespace(id=WINNER), answer=AsyncMock())
    await _handler("on_claim_button", event="callback_query")(
        callback, ClaimCb(giveaway_id=gid, tier_index=0), bot
    )
    callback.answer.assert_awaited_once_with(t("prize.not_drawn"), show_alert=True)

    message = _message(WINNER)
    await _handler("on_claim_command")(
        message, CommandObject(prefix="/", command="claim", args=str(gid)), bot, Mock()
    )
    message.answer.assert_awaited_once_with(t("prize.not_drawn"))

    outcome = await prize_service.claim(gid, WINNER, TIER)
    assert outcome.status == CLAIM_INELIGIBLE and outcome.code is None
    assert (await prize_service.stock(gid))[TIER].remaining == 1
    assert bot.sent == []

    # Previously stored claims must not expose the code through history either.
    async with sessionmaker() as session:
        code = await session.scalar(select(PrizeCode).where(PrizeCode.giveaway_id == gid))
        code.claimed_count = 1
        session.add(PrizeClaim(giveaway_id=gid, user_id=WINNER, tier_name=TIER, code_id=code.id))
        await session.commit()
    history = _message(WINNER)
    await _handler("on_myprizes")(history, bot, Mock())
    history.answer.assert_awaited_once_with(t("prize.myprizes_empty"))
    assert bot.sent == []


async def test_replaced_winner_and_wrong_tier_cannot_claim(sessionmaker, due_giveaway):
    gid = await due_giveaway(
        prize_mode="unique",
        prize_tiers=[{"name": TIER, "count": 1}, {"name": OTHER_TIER, "count": 1}],
    )
    await prize_service.add_codes(gid, TIER, ["NEW-WINNER-ONLY"])
    await prize_service.add_codes(gid, OTHER_TIER, ["ANOTHER-TIER-ONLY"])
    await _record_winners(sessionmaker, gid, [(WINNER, TIER)])
    async with sessionmaker() as session:
        await replace_tier_winners(session, gid, TIER)
        await log_winners(session, gid, [(OTHER_USER, TIER)])
        await session.commit()

    replaced = await prize_service.claim(gid, WINNER, TIER)
    wrong_tier = await prize_service.claim(gid, OTHER_USER, OTHER_TIER)
    assert replaced.status == wrong_tier.status == CLAIM_INELIGIBLE
    assert replaced.code is None and wrong_tier.code is None
    assert (await prize_service.stock(gid))[OTHER_TIER].remaining == 1
    legitimate = await prize_service.claim(gid, OTHER_USER, TIER)
    assert legitimate.status == CLAIM_OK and legitimate.code == "NEW-WINNER-ONLY"


async def test_early_legacy_award_and_claim_stay_invalid_after_actual_draw(
    sessionmaker, due_giveaway, bot
):
    gid = await due_giveaway(prize_mode="unique")
    await prize_service.add_codes(gid, TIER, ["OLD-ISSUED", "ACTUAL-WINNER"])
    async with sessionmaker() as session:
        (early,) = await log_winners(session, gid, [(WINNER, TIER)])
        code_id = await session.scalar(
            select(PrizeCode.id).where(PrizeCode.giveaway_id == gid, PrizeCode.code == "OLD-ISSUED")
        )
        session.add(PrizeClaim(giveaway_id=gid, user_id=WINNER, tier_name=TIER, code_id=code_id))
        await replace_tier_winners(session, gid, TIER)
        await log_winners(session, gid, [(1003, TIER)])
        await add_participant(session, giveaway_id=gid, user_id=OTHER_USER, username=None)
        await session.commit()

    await giveaway_service.finalize_and_publish(gid, bot)
    async with sessionmaker() as session:
        winners = await list_winners(session, gid)
        assert [winner.user_id for winner in winners] == [OTHER_USER]
        discarded = (await list_winners(session, gid, include_replaced=True))[0]
        assert discarded.id == early.id
        assert discarded.replaced_at == discarded.drawn_at
    assert (await prize_service.claim(gid, WINNER, TIER)).status == CLAIM_INELIGIBLE
    assert (await prize_service.claim(gid, 1003, TIER)).status == CLAIM_INELIGIBLE
    assert await prize_service.claimed_by(WINNER) == []
    assert (await prize_service.claim(gid, OTHER_USER, TIER)).code == "ACTUAL-WINNER"


async def test_concurrent_command_and_buttons_award_once_even_with_legacy_extra_win(
    sessionmaker, due_giveaway, bot
):
    gid = await due_giveaway(
        prize_mode="unique",
        prize_tiers=[{"name": TIER, "count": 1}, {"name": OTHER_TIER, "count": 1}],
    )
    await _record_winners(sessionmaker, gid, [(WINNER, TIER), (WINNER, OTHER_TIER)])
    await prize_service.add_codes(gid, TIER, ["FIRST-PRIZE"])
    await prize_service.add_codes(gid, OTHER_TIER, ["SECOND-PRIZE"])
    command = CommandObject(prefix="/", command="claim", args=str(gid))
    press = _handler("on_claim_button", event="callback_query")
    await asyncio.gather(
        _handler("on_claim_command")(_message(WINNER), command, bot, Mock()),
        *(
            press(
                SimpleNamespace(from_user=SimpleNamespace(id=WINNER), answer=AsyncMock()),
                ClaimCb(giveaway_id=gid, tier_index=index % 2),
                bot,
            )
            for index in range(10)
        ),
    )
    assert len(bot.sent) == 1 and bot.sent[0][0] == WINNER
    assert len(await prize_service.claimed_by(WINNER)) == 1
    assert sum(tier.remaining for tier in (await prize_service.stock(gid)).values()) == 1


async def test_independent_topups_ignore_duplicates_across_batches_and_tiers(
    sessionmaker, new_giveaway
):
    gid = await new_giveaway(
        prize_mode="unique",
        prize_tiers=[{"name": TIER, "count": 1}, {"name": OTHER_TIER, "count": 1}],
    )
    assert await prize_service.add_codes(gid, TIER, ["FIRST-CODE"]) == 1
    assert await prize_service.add_codes(gid, TIER, [" FIRST-CODE "]) == 0
    assert await prize_service.add_codes(gid, OTHER_TIER, ["FIRST-CODE", "SECOND-CODE"]) == 1
    async with sessionmaker() as session:
        rows = await session.execute(
            select(PrizeCode.tier_name, PrizeCode.code).where(PrizeCode.giveaway_id == gid)
        )
        assert set(rows) == {(TIER, "FIRST-CODE"), (OTHER_TIER, "SECOND-CODE")}


async def test_legacy_independent_duplicates_are_delivered_only_once(sessionmaker, due_giveaway):
    gid = await due_giveaway(
        prize_mode="unique",
        prize_tiers=[{"name": TIER, "count": 1}, {"name": OTHER_TIER, "count": 1}],
    )
    await _record_winners(sessionmaker, gid, [(WINNER, TIER), (OTHER_USER, OTHER_TIER)])
    async with sessionmaker() as session:
        session.add_all(
            PrizeCode(giveaway_id=gid, tier_name=tier, code="LEGACY-DUPLICATE", max_claims=1)
            for tier in (TIER, OTHER_TIER)
        )
        await session.commit()

    stock = await prize_service.stock(gid)
    assert stock[TIER].remaining == 1 and stock[OTHER_TIER].remaining == 0
    outcomes = await asyncio.gather(
        prize_service.claim(gid, WINNER, TIER),
        prize_service.claim(gid, OTHER_USER, OTHER_TIER),
    )
    assert {outcome.status for outcome in outcomes} == {CLAIM_OK, CLAIM_EMPTY}
    assert [outcome.code for outcome in outcomes if outcome.code] == ["LEGACY-DUPLICATE"]
    assert all(tier.remaining == 0 for tier in (await prize_service.stock(gid)).values())


async def test_shared_text_is_still_reusable(sessionmaker, due_giveaway):
    gid = await due_giveaway(prize_mode="shared", prize_tiers=[{"name": TIER, "count": 2}])
    await _record_winners(sessionmaker, gid, [(WINNER, TIER), (OTHER_USER, TIER)])
    shared = "SHARED-PRIZE"
    await prize_service.add_codes(
        gid, TIER, [shared], max_claims=prize_service.max_claims_for("shared")
    )
    first = await prize_service.claim(gid, WINNER, TIER)
    second = await prize_service.claim(gid, OTHER_USER, TIER)
    assert first.status == second.status == CLAIM_OK
    assert first.code == second.code == shared


async def test_unsent_prize_can_be_recovered_from_private_history(bot, sessionmaker, due_giveaway):
    gid = await due_giveaway(prize_mode="unique")
    await _record_winners(sessionmaker, gid, [(WINNER, TIER)])
    secret = "RECOVERABLE-PRIZE"
    await prize_service.add_codes(gid, TIER, [secret, "SECOND-PRIZE"])

    bot.fail_for.add(WINNER)
    callback = SimpleNamespace(from_user=SimpleNamespace(id=WINNER), answer=AsyncMock())
    await _handler("on_claim_button", event="callback_query")(
        callback, ClaimCb(giveaway_id=gid, tier_index=0), bot
    )
    callback.answer.assert_awaited_once_with(t("prize.dm_failed"), show_alert=True)
    assert bot.sent == []
    assert (await prize_service.claim(gid, WINNER, TIER)).status == CLAIM_ALREADY
    assert (await prize_service.stock(gid))[TIER].remaining == 1

    bot.fail_for.clear()
    await _handler("on_myprizes")(_message(WINNER), bot, Mock())
    assert len(bot.sent) == 1
    recipient, body, _ = bot.sent[0]
    assert recipient == WINNER
    assert secret in body and "SECOND-PRIZE" not in body


async def test_long_prize_history_is_delivered_as_a_private_file(bot, sessionmaker, due_giveaway):
    secrets = ["a" * 2400 + "&first", "b" * 2400 + "<second>"]
    for secret in secrets:
        gid = await due_giveaway(prize_mode="unique")
        await _record_winners(sessionmaker, gid, [(WINNER, TIER)])
        await prize_service.add_codes(gid, TIER, [secret])
        assert (await prize_service.claim(gid, WINNER, TIER)).status == CLAIM_OK

    bot.send_document = AsyncMock()
    await _handler("on_myprizes")(_message(WINNER), bot, Mock())
    bot.send_document.assert_awaited_once()
    recipient, document = bot.send_document.await_args.args
    assert recipient == WINNER
    assert isinstance(document, BufferedInputFile)
    assert all(secret in document.data.decode("utf-8") for secret in secrets)
    assert bot.sent == []


async def test_blacklisted_winner_cannot_claim_prize(bot, sessionmaker, due_giveaway):
    gid = await due_giveaway(prize_mode="unique")
    await prize_service.add_codes(gid, TIER, ["SECRET-FOR-CLEAN-WINNER"])
    await _record_winners(sessionmaker, gid, [(WINNER, TIER)])
    async with sessionmaker() as session:
        await ban_user(session, WINNER, reason="abuse", banned_by=1)
        await session.commit()

    callback = SimpleNamespace(from_user=SimpleNamespace(id=WINNER), answer=AsyncMock())
    await _handler("on_claim_button", event="callback_query")(
        callback, ClaimCb(giveaway_id=gid, tier_index=0), bot
    )
    callback.answer.assert_awaited_once_with(t("join.banned"), show_alert=True)

    message = _message(WINNER)
    await _handler("on_claim_command")(
        message, CommandObject(prefix="/", command="claim", args=str(gid)), bot, Mock()
    )
    message.answer.assert_awaited_once_with(t("join.banned"))

    outcome = await prize_service.claim(gid, WINNER, TIER)
    assert outcome.status == CLAIM_INELIGIBLE and outcome.code is None
    assert (await prize_service.stock(gid))[TIER].remaining == 1
    assert bot.sent == []


async def test_publishing_finished_giveaway_cannot_release_prize(bot, sessionmaker, due_giveaway):
    gid = await due_giveaway(prize_mode="unique", publishing=True)
    await prize_service.add_codes(gid, TIER, ["STILL-PUBLISHING"])
    await _record_winners(sessionmaker, gid, [(WINNER, TIER)])

    callback = SimpleNamespace(from_user=SimpleNamespace(id=WINNER), answer=AsyncMock())
    await _handler("on_claim_button", event="callback_query")(
        callback, ClaimCb(giveaway_id=gid, tier_index=0), bot
    )
    callback.answer.assert_awaited_once_with(t("prize.not_drawn"), show_alert=True)

    outcome = await prize_service.claim(gid, WINNER, TIER)
    assert outcome.status == CLAIM_INELIGIBLE and outcome.code is None
    assert bot.sent == []


async def test_shared_text_update_delivers_latest_text(sessionmaker, due_giveaway):
    gid = await due_giveaway(prize_mode="shared", prize_tiers=[{"name": TIER, "count": 2}])
    await _record_winners(sessionmaker, gid, [(WINNER, TIER), (OTHER_USER, TIER)])
    cap = prize_service.max_claims_for("shared")
    await prize_service.add_codes(gid, TIER, ["OLD-SHARED"], max_claims=cap)
    assert (await prize_service.claim(gid, WINNER, TIER)).code == "OLD-SHARED"
    async with sessionmaker() as session:
        old = await session.scalar(select(PrizeCode).where(PrizeCode.giveaway_id == gid))
        old.claimed_count = 0
        await session.commit()

    await prize_service.add_codes(gid, TIER, ["NEW-SHARED"], max_claims=cap)
    second = await prize_service.claim(gid, OTHER_USER, TIER)
    assert second.status == CLAIM_OK and second.code == "NEW-SHARED"


async def test_200_winners_concurrent_unique_code_claims_have_zero_duplicates(
    sessionmaker, due_giveaway
):
    tier_a = "一等奖"
    tier_b = "二等奖"
    gid = await due_giveaway(
        prize_mode="unique",
        prize_tiers=[{"name": tier_a, "count": 80}, {"name": tier_b, "count": 120}],
    )
    winners_a = [(20_000 + i, tier_a) for i in range(80)]
    winners_b = [(30_000 + i, tier_b) for i in range(120)]
    codes_a = [f"A-CODE-{i:04d}" for i in range(80)]
    codes_b = [f"B-CODE-{i:04d}" for i in range(120)]
    await _record_winners(sessionmaker, gid, [*winners_a, *winners_b])
    assert await prize_service.add_codes(gid, tier_a, codes_a) == 80
    assert await prize_service.add_codes(gid, tier_b, codes_b) == 120

    # Every one of the 200 winners fires two simultaneous claim requests (400 concurrent tasks).
    requests = [
        prize_service.claim(gid, uid, tier)
        for uid, tier in [*winners_a, *winners_b]
        for _ in range(2)
    ]
    outcomes = await asyncio.gather(*requests)

    ok_outcomes = [o for o in outcomes if o.status == CLAIM_OK]
    already_outcomes = [o for o in outcomes if o.status == CLAIM_ALREADY]
    assert len(ok_outcomes) == 200
    assert len(already_outcomes) == 200

    delivered_codes = [o.code for o in ok_outcomes]
    assert len(set(delivered_codes)) == 200
    assert set(delivered_codes) == set(codes_a) | set(codes_b)

    stock = await prize_service.stock(gid)
    assert stock[tier_a].claimed == 80 and stock[tier_a].remaining == 0
    assert stock[tier_b].claimed == 120 and stock[tier_b].remaining == 0

    async with sessionmaker() as session:
        claim_rows = (
            await session.execute(
                select(PrizeClaim.user_id, PrizeClaim.code_id).where(PrizeClaim.giveaway_id == gid)
            )
        ).all()
        code_counts = (
            await session.scalars(select(PrizeCode.claimed_count).where(PrizeCode.giveaway_id == gid))
        ).all()
    assert len(claim_rows) == 200
    assert len({uid for uid, _ in claim_rows}) == 200
    assert len({code_id for _, code_id in claim_rows}) == 200
    assert all(count == 1 for count in code_counts)


async def test_expired_undrawn_and_publishing_prizes_never_leak_through_claim_or_history(
    bot, sessionmaker, due_giveaway
):
    # Past end_at, still status=running (timer hasn't drawn yet): cannot claim.
    gid_undrawn = await due_giveaway(prize_mode="unique")
    await prize_service.add_codes(gid_undrawn, TIER, ["SECRET-BEFORE-DRAW"])
    await _record_winners(sessionmaker, gid_undrawn, [(WINNER, TIER)], status=GiveawayStatus.running)
    assert (await prize_service.claim(gid_undrawn, WINNER, TIER)).status == CLAIM_INELIGIBLE

    # Publishing=True even if status=finished: neither claim nor /myprizes may expose codes.
    gid_pub = await due_giveaway(prize_mode="unique", publishing=True)
    await prize_service.add_codes(gid_pub, TIER, ["SECRET-WHILE-PUBLISHING"])
    await _record_winners(sessionmaker, gid_pub, [(WINNER, TIER)], status=GiveawayStatus.finished)
    async with sessionmaker() as session:
        code = await session.scalar(select(PrizeCode).where(PrizeCode.giveaway_id == gid_pub))
        code.claimed_count = 1
        session.add(PrizeClaim(giveaway_id=gid_pub, user_id=WINNER, tier_name=TIER, code_id=code.id))
        await session.commit()

    history = _message(WINNER)
    await _handler("on_myprizes")(history, bot, Mock())
    history.answer.assert_awaited_once_with(t("prize.myprizes_empty"))
    assert bot.sent == []
