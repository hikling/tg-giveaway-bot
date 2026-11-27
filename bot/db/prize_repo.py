from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Giveaway, GiveawayStatus, PrizeClaim, PrizeCode, WinnerLog
from bot.db.repo import claim_giveaway, is_blacklisted
from bot.prize_policy import CLAIM_WINDOW, SELF_SERVICE_MODES
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
#: The winner did not claim within the round's claim window.
CLAIM_EXPIRED = "expired"

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
    recoverable: int = 0
    withdrawn: int = 0

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


@dataclass(frozen=True, slots=True)
class WinnerClaim:
    drawn_at: datetime
    claimed: bool

    @property
    def deadline(self) -> datetime:
        return self.drawn_at + CLAIM_WINDOW


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
        if codes:
            # 共享文本更新时移除尚未领取的旧行，并将已领取旧行封顶，确保后续中奖者拿到最新文本。
            await session.execute(
                delete(PrizeCode).where(
                    PrizeCode.giveaway_id == giveaway_id,
                    PrizeCode.tier_name == tier,
                    PrizeCode.claimed_count == 0,
                )
            )
            await session.execute(
                update(PrizeCode)
                .where(
                    PrizeCode.giveaway_id == giveaway_id,
                    PrizeCode.tier_name == tier,
                    PrizeCode.claimed_count > 0,
                )
                .values(max_claims=PrizeCode.claimed_count)
            )
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
    claimed_code_ids = select(PrizeClaim.code_id).where(PrizeClaim.giveaway_id == giveaway_id)
    unavailable = select(PrizeCode.code).where(
        PrizeCode.giveaway_id == giveaway_id,
        (PrizeCode.claimed_count > 0)
        | PrizeCode.id.in_(claimed_code_ids)
        | PrizeCode.withdrawn_at.is_not(None),
    )
    return (
        select(func.min(PrizeCode.id))
        .where(
            PrizeCode.giveaway_id == giveaway_id,
            PrizeCode.claimed_count == 0,
            PrizeCode.claimed_count < PrizeCode.max_claims,
            PrizeCode.code.not_in(unavailable),
        )
        .group_by(PrizeCode.code)
    )


def _winner_has_claim():
    return (
        select(PrizeClaim.id)
        .where(
            PrizeClaim.giveaway_id == WinnerLog.giveaway_id,
            PrizeClaim.user_id == WinnerLog.user_id,
            PrizeClaim.tier_name == WinnerLog.tier_name,
        )
        .exists()
    )


def _recoverable_tiers(giveaway_id: int, now: datetime):
    """Tiers with expired unclaimed awards and no remaining open claim windows."""
    cutoff = now - CLAIM_WINDOW
    open_award = WinnerLog.replaced_at.is_(None) & (WinnerLog.drawn_at > cutoff)
    return (
        select(WinnerLog.tier_name)
        .where(
            WinnerLog.giveaway_id == giveaway_id,
            ~_winner_has_claim(),
        )
        .group_by(WinnerLog.tier_name)
        .having(
            func.min(WinnerLog.drawn_at) <= cutoff,
            func.sum(case((open_award, 1), else_=0)) == 0,
        )
    )


async def withdraw_recovered_codes(
    session: AsyncSession, giveaway_id: int, creator_id: int
) -> list[str] | None:
    """Move recovered codes out of awardable stock and keep them for repeat downloads."""
    giveaway = await claim_giveaway(session, giveaway_id, GiveawayStatus.finished)
    if (
        giveaway is None
        or giveaway.publishing
        or giveaway.creator_id != creator_id
        or giveaway.prize_mode != "unique"
    ):
        return None

    now = datetime.now(UTC)
    await session.execute(
        update(PrizeCode)
        .where(
            PrizeCode.giveaway_id == giveaway_id,
            PrizeCode.tier_name.in_(_recoverable_tiers(giveaway_id, now)),
            PrizeCode.id.in_(_claimable_unique_codes(giveaway_id)),
        )
        .values(withdrawn_at=now)
        .execution_options(synchronize_session=False)
    )
    return list(
        await session.scalars(
            select(PrizeCode.code)
            .where(PrizeCode.giveaway_id == giveaway_id, PrizeCode.withdrawn_at.is_not(None))
            .order_by(PrizeCode.id)
        )
    )


async def stock_by_tier(session: AsyncSession, giveaway_id: int) -> dict[str, TierStock]:
    """Inventory and recovered codes per tier, for the admin panel."""
    giveaway = await session.get(Giveaway, giveaway_id)
    if giveaway is None:
        return {}
    recoverable_tiers: set[str] = set()
    total = func.sum(PrizeCode.max_claims)
    if giveaway.prize_mode == "unique":
        available = case((PrizeCode.id.in_(_claimable_unique_codes(giveaway_id)), 1), else_=0)
        total = func.sum(PrizeCode.claimed_count + available)
        if giveaway.status == GiveawayStatus.finished and not giveaway.publishing:
            recoverable_tiers = set(
                await session.scalars(_recoverable_tiers(giveaway_id, datetime.now(UTC)))
            )
    rows = (
        await session.execute(
            select(
                PrizeCode.tier_name,
                total,
                func.sum(PrizeCode.claimed_count),
                func.count(PrizeCode.withdrawn_at),
            )
            .where(PrizeCode.giveaway_id == giveaway_id)
            .group_by(PrizeCode.tier_name)
        )
    ).all()
    stock = {}
    for name, total_count, claimed_count, withdrawn in rows:
        total_count, claimed_count = int(total_count or 0), int(claimed_count or 0)
        recoverable = max(total_count - claimed_count, 0) if name in recoverable_tiers else 0
        stock[name] = TierStock(name, total_count, claimed_count, recoverable, withdrawn)
    return stock


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


async def current_winner_claims(
    session: AsyncSession, giveaway_id: int, user_id: int
) -> dict[str, WinnerClaim]:
    rows = await session.execute(
        select(WinnerLog.tier_name, WinnerLog.drawn_at, _winner_has_claim().label("claimed"))
        .where(
            WinnerLog.giveaway_id == giveaway_id,
            WinnerLog.user_id == user_id,
            WinnerLog.replaced_at.is_(None),
        )
        .order_by(WinnerLog.drawn_at)
    )
    return {tier: WinnerClaim(drawn_at, claimed) for tier, drawn_at, claimed in rows}


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
        or gw.prize_mode not in SELF_SERVICE_MODES
        or await is_blacklisted(session, user_id)
    ):
        return ClaimOutcome(CLAIM_INELIGIBLE)

    winner = (await current_winner_claims(session, giveaway_id, user_id)).get(tier)
    if winner is None:
        return ClaimOutcome(CLAIM_INELIGIBLE)
    if winner.claimed:
        return ClaimOutcome(CLAIM_ALREADY)
    now = datetime.now(UTC)
    if winner.deadline <= now:
        return ClaimOutcome(CLAIM_EXPIRED)

    available = select(PrizeCode).where(
        PrizeCode.giveaway_id == giveaway_id,
        PrizeCode.tier_name == tier,
        PrizeCode.claimed_count < PrizeCode.max_claims,
    )
    if gw.prize_mode == "unique":
        # A pre-upgrade database may already contain repeated copies.
        available = available.where(
            PrizeCode.claimed_count == 0,
            PrizeCode.id.in_(_claimable_unique_codes(giveaway_id)),
        )
    row = await session.scalar(available.order_by(PrizeCode.id).limit(1))
    if row is None:
        return ClaimOutcome(CLAIM_EMPTY)

    take_stmt = update(PrizeCode).where(
        PrizeCode.id == row.id,
        PrizeCode.claimed_count < PrizeCode.max_claims,
    )
    if gw.prize_mode == "unique":
        take_stmt = take_stmt.where(PrizeCode.claimed_count == 0)
    taken = await session.execute(take_stmt.values(claimed_count=PrizeCode.claimed_count + 1))
    if taken.rowcount != 1:
        return ClaimOutcome(CLAIM_EMPTY)

    session.add(
        PrizeClaim(
            giveaway_id=giveaway_id,
            tier_name=tier,
            user_id=user_id,
            code_id=row.id,
            claimed_at=now,
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
                Giveaway.publishing.is_(False),
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
