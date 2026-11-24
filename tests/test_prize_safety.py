from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType
from aiogram.filters import CommandObject
from aiogram.types import BufferedInputFile
from sqlalchemy import select

from bot.callbacks.factories import ClaimCb
from bot.db.models import Giveaway, GiveawayStatus, PrizeClaim, PrizeCode
from bot.db.prize_repo import CLAIM_ALREADY, CLAIM_EMPTY, CLAIM_INELIGIBLE, CLAIM_OK
from bot.db.repo import log_winners, replace_tier_winners
from bot.handlers.prize import build_prize_router
from bot.i18n import t
from bot.services import prize as prize_service

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
    return SimpleNamespace(
        from_user=SimpleNamespace(id=uid),
        chat=SimpleNamespace(
            id=-1001002003004 if group else uid,
            type=ChatType.SUPERGROUP if group else ChatType.PRIVATE,
        ),
        answer=AsyncMock(),
    )


async def _record_winners(sessionmaker, gid, winners, *, status=GiveawayStatus.finished):
    async with sessionmaker() as session:
        gw = await session.get(Giveaway, gid)
        gw.status = status
        await log_winners(session, gid, winners)
        await session.commit()


async def test_prize_history_stays_in_the_winners_private_chat(bot, sessionmaker, due_giveaway):
    gid = await due_giveaway(prize_mode="unique")
    await _record_winners(sessionmaker, gid, [(WINNER, TIER)])
    secret = "WINNER-PRIVATE-CODE"
    assert await prize_service.add_codes(gid, TIER, [secret]) == 1
    assert (await prize_service.claim(gid, WINNER, TIER)).status == CLAIM_OK

    history = _handler("on_myprizes")
    group = _message(WINNER, group=True)
    await history(group, bot)
    group.answer.assert_awaited_once_with(t("prize.private_history"))
    assert bot.sent == []

    unrelated = _message(OTHER_USER)
    await history(unrelated, bot)
    unrelated.answer.assert_awaited_once_with(t("prize.myprizes_empty"))
    assert bot.sent == []

    private = _message(WINNER)
    await history(private, bot)
    private.answer.assert_not_called()
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
        message, CommandObject(prefix="/", command="claim", args=str(gid)), bot
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
    await _handler("on_myprizes")(history, bot)
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
    await _handler("on_myprizes")(_message(WINNER), bot)
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
    await _handler("on_myprizes")(_message(WINNER), bot)
    bot.send_document.assert_awaited_once()
    recipient, document = bot.send_document.await_args.args
    assert recipient == WINNER
    assert isinstance(document, BufferedInputFile)
    assert all(secret in document.data.decode("utf-8") for secret in secrets)
    assert bot.sent == []
