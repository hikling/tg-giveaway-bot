from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Giveaway, GiveawayStatus, PrizeClaim, PrizeCode, WinnerLog
from bot.db.repo import claim_giveaway
from bot.utils.prize_input import normalize_codes

log = logging.getLogger("bot.db.prize_repo")

#: The winner got their prize.
CLAIM_OK = "ok"
#: This winner already claimed this tier.
CLAIM_ALREADY = "already"
#: Nothing left in stock for that tier.
CLAIM_EMPTY = "empty"
#: This giveaway is not finished, or the user no longer holds this tier.
CLAIM_INELIGIBLE = "ineligible"

#: How many rows a shared-text giveaway consumes.  One row with a huge cap
#: keeps the delivery code identical for both modes.
SHARED_CAP = 1_000_000_000


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    """Result of one claim attempt."""

    status: str
    code: str | None = None


@dataclass(frozen=True, slots=True)
class TierStock:
    """Inventory snapshot for one prize tier."""

    tier_name: str
    total: int
    claimed: int

    @property
    def remaining(self) -> int:
        return max(self.total - self.claimed, 0)


@dataclass(frozen=True, slots=True)
class PrizeView:
    """One row of /myprizes."""

    giveaway_id: int
    giveaway_title: str
    tier_name: str
    code: str
    claimed_at: datetime | None


async def new_unique_codes(
    session: AsyncSession, giveaway_id: int, codes: list[str]
) -> list[str]:
    """Keep independent codes that aren't already stocked in this giveaway."""
    normalized = normalize_codes(codes)
    if not normalized:
        return []
    stocked = set(
        await session.scalars(select(PrizeCode.code).where(PrizeCode.giveaway_id == giveaway_id))
    )
    return [code for code in normalized if code not in stocked]


async def add_codes(
    session: AsyncSession,
    giveaway_id: int,
    tier_name: str,
    codes: list[str],
    *,
    max_claims: int = 1,
) -> int:
    """Append codes to the stock of one tier; returns how many were added.

    Independent codes are unique across all tiers of one giveaway. Shared
    text can be stocked again. Callers hold the giveaway's write lock, or
    are creating the giveaway in this transaction.
    """
    tier = tier_name.strip()[:64]
    max_claims = max(int(max_claims), 1)
    if max_claims == 1:
        codes = await new_unique_codes(session, giveaway_id, codes)
    else:
        codes = normalize_codes(codes)
    for code in codes:
        session.add(
            PrizeCode(
                giveaway_id=giveaway_id,
                tier_name=tier,
                code=code,
                max_claims=max_claims,
                claimed_count=0,
            )
        )
    if codes:
        await session.flush()
    return len(codes)


def _claimable_unique_codes(giveaway_id: int):
    """Only the first stocked copy of each unused code is available."""
    issued = select(PrizeCode.code).where(
        PrizeCode.giveaway_id == giveaway_id, PrizeCode.claimed_count > 0
    )
    return (
        select(func.min(PrizeCode.id))
        .where(
            PrizeCode.giveaway_id == giveaway_id,
            PrizeCode.claimed_count < PrizeCode.max_claims,
            PrizeCode.code.not_in(issued),
        )
        .group_by(PrizeCode.code)
    )


async def stock_by_tier(session: AsyncSession, giveaway_id: int) -> dict[str, TierStock]:
    """Total / claimed / remaining per tier, for the admin panel."""
    mode = await session.scalar(select(Giveaway.prize_mode).where(Giveaway.id == giveaway_id))
    total = func.sum(PrizeCode.max_claims)
    if mode == "unique":
        available = case((PrizeCode.id.in_(_claimable_unique_codes(giveaway_id)), 1), else_=0)
        total = func.sum(PrizeCode.claimed_count + available)
    rows = (
        await session.execute(
            select(
                PrizeCode.tier_name,
                total,
                func.sum(PrizeCode.claimed_count),
            )
            .where(PrizeCode.giveaway_id == giveaway_id)
            .group_by(PrizeCode.tier_name)
        )
    ).all()
    return {
        name: TierStock(name, int(total or 0), int(claimed or 0)) for name, total, claimed in rows
    }


async def has_claim(
    session: AsyncSession, *, giveaway_id: int, tier_name: str, user_id: int | None = None
) -> bool:
    query = select(PrizeClaim.id).where(
        PrizeClaim.giveaway_id == giveaway_id, PrizeClaim.tier_name == tier_name.strip()[:64]
    )
    if user_id is not None:
        query = query.where(PrizeClaim.user_id == user_id)
    return await session.scalar(query.limit(1)) is not None


async def current_winner_tiers(
    session: AsyncSession, giveaway_id: int, user_id: int
) -> set[str]:
    tiers = await session.scalars(
        select(WinnerLog.tier_name).where(
            WinnerLog.giveaway_id == giveaway_id,
            WinnerLog.user_id == user_id,
            WinnerLog.replaced_at.is_(None),
        )
    )
    return set(tiers)


async def claim_code(
    session: AsyncSession, *, giveaway_id: int, user_id: int, tier_name: str
) -> ClaimOutcome:
    """Hand one code to one winner, exactly once.

    Hold the same giveaway write lock as rerolling from the eligibility check
    through stock reservation and claim creation.
    """
    tier = tier_name.strip()[:64]
    gw = await claim_giveaway(session, giveaway_id, GiveawayStatus.finished)
    if (
        gw is None
        or gw.publishing
        or gw.prize_mode not in {"unique", "shared"}
        or tier not in await current_winner_tiers(session, giveaway_id, user_id)
    ):
        return ClaimOutcome(CLAIM_INELIGIBLE)

    if await has_claim(session, giveaway_id=giveaway_id, user_id=user_id, tier_name=tier):
        return ClaimOutcome(CLAIM_ALREADY)

    available = select(PrizeCode).where(
        PrizeCode.giveaway_id == giveaway_id,
        PrizeCode.tier_name == tier,
        PrizeCode.claimed_count < PrizeCode.max_claims,
    )
    if gw.prize_mode == "unique":
        # A pre-upgrade database may already contain repeated copies.
        available = available.where(PrizeCode.id.in_(_claimable_unique_codes(giveaway_id)))
    row = await session.scalar(available.order_by(PrizeCode.id).limit(1))
    if row is None:
        return ClaimOutcome(CLAIM_EMPTY)

    taken = await session.execute(
        update(PrizeCode)
        .where(
            PrizeCode.id == row.id,
            PrizeCode.claimed_count < PrizeCode.max_claims,
        )
        .values(claimed_count=PrizeCode.claimed_count + 1)
    )
    if taken.rowcount != 1:
        return ClaimOutcome(CLAIM_EMPTY)

    session.add(
        PrizeClaim(
            giveaway_id=giveaway_id,
            tier_name=tier,
            user_id=user_id,
            code_id=row.id,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        # Same winner, same tier, twice: rollback also undoes the counter.
        await session.rollback()
        return ClaimOutcome(CLAIM_ALREADY)
    return ClaimOutcome(CLAIM_OK, row.code)


async def my_prizes(session: AsyncSession, user_id: int) -> list[PrizeView]:
    """Everything this user has already claimed, newest first."""
    rows = (
        await session.execute(
            select(
                PrizeClaim.giveaway_id,
                Giveaway.title,
                PrizeClaim.tier_name,
                PrizeCode.code,
                PrizeClaim.claimed_at,
            )
            .join(PrizeCode, PrizeCode.id == PrizeClaim.code_id)
            .join(Giveaway, Giveaway.id == PrizeClaim.giveaway_id)
            .where(
                PrizeClaim.user_id == user_id,
                Giveaway.status == GiveawayStatus.finished,
                PrizeCode.giveaway_id == PrizeClaim.giveaway_id,
                PrizeCode.tier_name == PrizeClaim.tier_name,
            )
            .order_by(PrizeClaim.claimed_at.desc(), PrizeClaim.id.desc())
        )
    ).all()
    return [
        PrizeView(
            giveaway_id=gid,
            giveaway_title=title,
            tier_name=tier,
            code=code,
            claimed_at=claimed_at,
        )
        for gid, title, tier, code, claimed_at in rows
    ]
