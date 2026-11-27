from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from weakref import WeakValueDictionary

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from bot.callbacks.factories import ClaimCb
from bot.db.base import get_sessionmaker
from bot.db.models import GiveawayStatus, User, WinnerLog
from bot.db.repo import (
    GiveawayView,
    TierShortfall,
    get_giveaway_view,
    list_winners,
    mark_winner_notified,
    pending_notifications,
)
from bot.i18n import t
from bot.prize_policy import CLAIM_WINDOW, SELF_SERVICE_MODES
from bot.services.prize import claim_deadline_notice, claim_window_notice
from bot.settings import get_settings
from bot.utils.esc import user_mention

log = logging.getLogger("bot.services.notifier")

MAX_SEND_RETRIES = 2
_winner_delivery_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


def winner_delivery_lock(giveaway_id: int) -> asyncio.Lock:
    """Order draws, rerolls and retries for one giveaway in the bot process."""
    return _winner_delivery_locks.setdefault(giveaway_id, asyncio.Lock())


async def _send_with_retry(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    for attempt in range(MAX_SEND_RETRIES + 1):
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return
        except TelegramRetryAfter as exc:
            if attempt >= MAX_SEND_RETRIES:
                raise
            delay = max(float(getattr(exc, "retry_after", 1) or 1), 0.1)
            log.warning("Telegram flood limit for chat %s, sleeping %.1fs", chat_id, delay)
            await asyncio.sleep(delay)


def _build_announcement(
    giveaway: GiveawayView,
    winner_count: int,
    *,
    shortfalls: Sequence[TierShortfall],
    reason: str,
    reveal_round: int | None,
    pool_digest: str | None,
    lang: str,
) -> str:
    header = "giveaway.finished_header" if reason == "draw" else "giveaway.reroll_header"
    lines = [
        t(header, lang, id=giveaway.id),
        t("giveaway.result_title", lang, title=giveaway.title),
        "",
        t("giveaway.result_count", lang, count=winner_count),
    ]
    missing = sum(short.missing for short in shortfalls)
    if missing:
        lines.append(t("giveaway.result_shortfall", lang, missing=missing))
    lines.extend([
        "",
        t("giveaway.result_check", lang, id=giveaway.id),
        claim_window_notice(giveaway.prize_mode, lang),
    ])
    if giveaway.draw_commit:
        proof = t(
            "giveaway.reveal_line",
            lang,
            seed=giveaway.draw_seed or "-",
            commit=giveaway.draw_commit,
            digest=pool_digest or "-",
            round=reveal_round or 1,
        ).strip()
        lines.extend(["", f"<blockquote expandable>{proof}</blockquote>"])
    return "\n".join(lines)


async def _winner_langs(session, winner_ids: Sequence[int]) -> dict[int, str]:
    if not winner_ids:
        return {}
    stmt = select(User).where(User.user_id.in_(list(winner_ids)))
    return {u.user_id: u.lang for u in (await session.execute(stmt)).scalars().all() if u.lang}


def _claim_keyboard(giveaway: GiveawayView, tier: str, lang: str) -> InlineKeyboardMarkup | None:
    """The "claim my prize" button, or None when the host delivers by hand.

    The tier travels as its position in ``prize_tiers``; a tier that no longer
    exists (rerolled away, edited post) yields no button rather than a dead
    one that would do nothing when pressed.
    """
    if giveaway.prize_mode not in SELF_SERVICE_MODES:
        return None
    tiers = list(giveaway.prize_tiers or ())
    index = next((i for i, item in enumerate(tiers) if str(item.get("name")) == tier), None)
    if index is None:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("button.claim", lang),
                    callback_data=ClaimCb(giveaway_id=giveaway.id, tier_index=index).pack(),
                )
            ]
        ]
    )


async def _notify_winner(
    bot: Bot, giveaway: GiveawayView, winner: WinnerLog, lang: str, creator_username: str | None
) -> bool:
    deadline = winner.drawn_at + CLAIM_WINDOW
    self_service = giveaway.prize_mode in SELF_SERVICE_MODES
    if self_service and deadline <= datetime.now(UTC):
        return False

    creator = (
        f"@{creator_username}"
        if creator_username
        else user_mention(giveaway.creator_id, t("winner_dm.host", lang, id=giveaway.creator_id))
    )
    key = "winner_dm.body_claim" if self_service else "winner_dm.body"
    body = t(
        key, lang, title=giveaway.title, tier=winner.tier_name, creator=creator, id=giveaway.id
    ).strip()
    lines = [body, "", claim_window_notice(giveaway.prize_mode, lang)]
    if self_service:
        lines.append(claim_deadline_notice(deadline, lang))

    error = None
    try:
        await _send_with_retry(
            bot,
            winner.user_id,
            "\n".join(lines),
            reply_markup=_claim_keyboard(giveaway, winner.tier_name, lang),
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.warning("winner DM failed for %d: %s", winner.user_id, error)
    async with get_sessionmaker()() as session:
        await mark_winner_notified(session, winner.id, ok=error is None, error=error)
        await session.commit()
    return error is None


async def publish_winners(
    bot: Bot,
    giveaway: GiveawayView,
    winners: Sequence[tuple[int, str]],
    *,
    shortfalls: Sequence[TierShortfall] = (),
    reason: str = "draw",
    reveal_round: int | None = None,
    pool_digest: str | None = None,
) -> None:
    """Announce and notify a committed draw while holding its delivery lock.

    ``giveaway`` is a frozen :class:`GiveawayView`, not a live ORM row —
    the caller has already left its session, and relying on
    ``expire_on_commit=False`` to keep a detached instance readable was an
    invisible contract that breaks the moment someone aligns the
    sessionmaker with the SQLAlchemy default.

    Every DM outcome is now persisted (``notified_ok`` / ``notify_error`` /
    ``notified_at``) via ``mark_winner_notified``; previously the result was
    logged and forgotten, so a failed delivery left no trace and no way to
    retry selectively.
    """
    if giveaway.status != GiveawayStatus.finished:
        return
    default_lang = get_settings().default_lang
    drawn = set(winners)
    async with get_sessionmaker()() as session:
        creator = await session.get(User, giveaway.creator_id)
        creator_username = creator.username if creator else None
        current = await list_winners(session, giveaway.id) if drawn else ()
        rows = [
            row for row in current if (row.user_id, row.tier_name) in drawn
        ]
        winner_langs = await _winner_langs(session, [row.user_id for row in rows])

    # 1. public announcement, replied under the giveaway post
    announcement = _build_announcement(
        giveaway,
        len(winners),
        shortfalls=shortfalls,
        reason=reason,
        reveal_round=reveal_round,
        pool_digest=pool_digest,
        lang=default_lang,
    )

    from bot.services.publication import (
        cancel_post_count_sync,
        post_locations,
        refresh_giveaway_post_count,
    )

    cancel_post_count_sync(giveaway.id)
    await refresh_giveaway_post_count(bot, giveaway.id, allow_finished=True)

    for chat_id, message_id in await post_locations(
        giveaway.id, giveaway.chat_id, giveaway.message_id
    ):
        try:
            await _send_with_retry(
                bot,
                chat_id,
                announcement,
                reply_to_message_id=message_id,
            )
        except Exception:
            log.exception("publish_winners: send_message failed")

    # 2. private winner notifications, each with a persisted outcome
    for row in rows:
        winner_lang = winner_langs.get(row.user_id) or default_lang
        await _notify_winner(bot, giveaway, row, winner_lang, creator_username)


async def retry_pending(bot: Bot, giveaway_id: int) -> int:
    """Re-send DMs for winners whose delivery was never confirmed.

    Includes both unattempted and failed current winners. Returns the
    number of messages actually sent; superseded winners are never retried.
    """
    async with winner_delivery_lock(giveaway_id):
        return await _retry_pending(bot, giveaway_id)


async def _retry_pending(bot: Bot, giveaway_id: int) -> int:
    async with get_sessionmaker()() as session:
        giveaway = await get_giveaway_view(session, giveaway_id)
        if giveaway is None or giveaway.status != GiveawayStatus.finished:
            return 0
        rows = await pending_notifications(session, giveaway_id)
        if not rows:
            return 0
        creator = await session.get(User, giveaway.creator_id)
        creator_username = creator.username if creator else None
        langs = await _winner_langs(session, [row.user_id for row in rows])

    default_lang = get_settings().default_lang
    sent = 0
    for row in rows:
        winner_lang = langs.get(row.user_id) or default_lang
        sent += await _notify_winner(bot, giveaway, row, winner_lang, creator_username)
    log.info("retry_pending: re-sent %d/%d winner DMs", sent, len(rows))
    return sent
