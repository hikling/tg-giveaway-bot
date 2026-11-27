from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from bot.db import prize_repo
from bot.db.base import get_sessionmaker
from bot.db.prize_repo import CLAIM_OK, SHARED_CAP, ClaimOutcome, PrizeView, TierStock
from bot.db.repo import claim_giveaway
from bot.i18n import t
from bot.prize_policy import CLAIM_WINDOW_HOURS, SELF_SERVICE_MODES
from bot.settings import get_settings
from bot.utils.timeparse import format_local


def claim_window_notice(prize_mode: str, lang: str | None = None) -> str:
    key = "prize.claim_window" if prize_mode in SELF_SERVICE_MODES else "prize.manual_window"
    return t(key, lang, hours=CLAIM_WINDOW_HOURS)


def claim_deadline_notice(deadline: datetime, lang: str | None = None) -> str:
    tz = get_settings().tz
    label = "北京时间" if tz == "Asia/Shanghai" else tz
    return t("prize.claim_deadline", lang, deadline=format_local(deadline, tz, seconds=True), tz=label)


def tier_at(prize_tiers: Sequence[dict[str, Any]], index: int) -> dict[str, Any] | None:
    """Resolve a tier by position, tolerating a stale button."""
    if 0 <= index < len(prize_tiers):
        return dict(prize_tiers[index])
    return None


def max_claims_for(mode: str) -> int:
    """A shared text is one row every winner may consume; a code is one row."""
    return SHARED_CAP if mode == "shared" else 1


async def add_codes(
    giveaway_id: int, tier_name: str, codes: list[str], *, max_claims: int = 1
) -> int:
    sm = get_sessionmaker()
    async with sm() as session:
        if await claim_giveaway(session, giveaway_id, None) is None:
            return 0
        added = await prize_repo.add_codes(
            session, giveaway_id, tier_name, codes, max_claims=max_claims
        )
        await session.commit()
        return added


async def new_stock_count(giveaway_id: int, mode: str, codes: list[str]) -> int:
    """Match inventory de-duplication when previewing a top-up."""
    if mode == "unique":
        async with get_sessionmaker()() as session:
            return len(await prize_repo.new_unique_codes(session, giveaway_id, codes))
    return len(codes)


async def claim(giveaway_id: int, user_id: int, tier_name: str) -> ClaimOutcome:
    """Commit the authorized claim and its stock decrement together."""
    sm = get_sessionmaker()
    async with sm() as session:
        outcome = await prize_repo.claim_code(
            session, giveaway_id=giveaway_id, user_id=user_id, tier_name=tier_name
        )
        if outcome.status == CLAIM_OK:
            await session.commit()
        else:
            await session.rollback()
        return outcome


async def stock(giveaway_id: int) -> dict[str, TierStock]:
    sm = get_sessionmaker()
    async with sm() as session:
        return await prize_repo.stock_by_tier(session, giveaway_id)


async def withdraw_recovered_codes(giveaway_id: int, creator_id: int) -> list[str] | None:
    async with get_sessionmaker()() as session:
        codes = await prize_repo.withdraw_recovered_codes(session, giveaway_id, creator_id)
        if codes is not None:
            await session.commit()
        return codes


async def claimed_by(user_id: int) -> list[PrizeView]:
    sm = get_sessionmaker()
    async with sm() as session:
        return await prize_repo.my_prizes(session, user_id)


async def won_tiers(giveaway_id: int, user_id: int, prize_tiers: Sequence[dict[str, Any]]) -> list[str]:
    """Tier names this user currently holds for one giveaway, in tier order."""
    sm = get_sessionmaker()
    async with sm() as session:
        held = await prize_repo.current_winner_tiers(session, giveaway_id, user_id)
    return [str(tier.get("name")) for tier in prize_tiers if str(tier.get("name")) in held]
