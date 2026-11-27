from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select

from bot.db.base import get_sessionmaker
from bot.db.models import Giveaway, GiveawayStatus
from bot.db.repo import list_running_giveaways

log = logging.getLogger("bot.services.scheduler")
_GROUP_MESSAGE_LIFETIME = timedelta(seconds=10)


def _as_utc(value: datetime, giveaway_id: int) -> datetime:
    """Accept a naive datetime and treat it as UTC instead of crashing.

    SQLite hands back naive datetimes even for ``DateTime(timezone=True)``,
    so ``max(end_at, now)`` used to raise
    ``TypeError: can't compare offset-naive and offset-aware datetimes``
    and killed every restart that had a giveaway pending.  The models now use
    ``UTCDateTime`` (which always returns aware values), but the scheduler is
    the last line of defence for rows written by older builds.
    """
    if value.tzinfo is None:
        log.warning(
            "giveaway %s has a naive end_at (%s); assuming UTC",
            giveaway_id,
            value.isoformat(),
        )
        return value.replace(tzinfo=UTC)
    return value


class GiveawayScheduler:
    def __init__(self, bot: Bot, tz: str = "UTC") -> None:
        self._bot = bot
        self._tz = tz
        self._scheduler = AsyncIOScheduler(timezone=tz)
        self._shutdown_requested = False

    def start(self) -> None:
        from bot.services.poetry import expire_due_questions
        from bot.services.publication import refresh_running_post_counts

        self._scheduler.add_job(
            expire_due_questions,
            "interval",
            seconds=1,
            id="poetry-timeouts",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            kwargs={"bot": self._bot},
        )
        self._scheduler.add_job(
            refresh_running_post_counts,
            "interval",
            minutes=5,
            id="giveaway-participant-counts",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            kwargs={"bot": self._bot},
        )
        self._scheduler.add_job(
            self.finalize_overdue,
            "interval",
            minutes=1,
            id="giveaway-overdue-draws",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self._shutdown_requested = False
        self._scheduler.start()
        log.info("scheduler started (tz=%s)", self._tz)

    def shutdown(self) -> None:
        if self._shutdown_requested or not self._scheduler.running:
            return
        self._shutdown_requested = True
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:
            log.exception("scheduler shutdown failed")

    async def restore_from_db(self) -> None:
        sm = get_sessionmaker()
        async with sm() as session:
            running = await list_running_giveaways(session)
        log.info("restoring %d running giveaway(s)", len(running))
        from bot.services.giveaway import restore_count_draw
        from bot.services.poetry import expire_due_questions
        from bot.services.publication import refresh_running_post_counts

        for gw in running:
            if gw.publishing:
                from bot.services.publication import publish_all

                result = await publish_all(self._bot, gw.id)
                if result.canceled:
                    continue
            self.schedule(gw.id, gw.end_at, gw.draw_mode)
            if gw.draw_mode in {"count", "either"}:
                await restore_count_draw(gw.id, self._bot)
        await expire_due_questions(self._bot)
        await refresh_running_post_counts(self._bot)

    async def finalize_overdue(self) -> None:
        """Retry due time draws that a failed or missed one-off job left open."""
        from bot.services.giveaway import finalize_and_publish

        async with get_sessionmaker()() as session:
            overdue = (
                await session.scalars(
                    select(Giveaway.id).where(
                        Giveaway.status == GiveawayStatus.running,
                        Giveaway.publishing.is_(False),
                        Giveaway.draw_mode.in_(("time", "either")),
                        Giveaway.end_at <= datetime.now(UTC),
                    )
                )
            ).all()
        for gid in overdue:
            try:
                await finalize_and_publish(gid, self._bot)
            except Exception:
                log.exception("retrying overdue draw %s failed", gid)

    def schedule(self, giveaway_id: int, end_at: datetime, draw_mode: str = "time") -> None:
        from bot.services import giveaway as gw_service

        if draw_mode == "count":
            return
        run_at = _as_utc(end_at, giveaway_id)
        if run_at <= datetime.now(UTC):
            # A giveaway restored from the DB whose deadline already passed
            # must fire immediately rather than being scheduled for the past
            # (APScheduler would refuse it outright).
            run_at = datetime.now(UTC)
        job_id = f"giveaway-{giveaway_id}"
        self._scheduler.add_job(
            gw_service.finalize_and_publish,
            trigger=DateTrigger(run_date=run_at, timezone=self._tz),
            id=job_id,
            replace_existing=True,
            misfire_grace_time=None,
            kwargs={"giveaway_id": giveaway_id, "bot": self._bot},
        )
        log.info("scheduled %s at %s", job_id, run_at.isoformat())

    def schedule_group_message_deletion(self, chat_id: int, message_id: int) -> None:
        self._scheduler.add_job(
            self._delete_group_message,
            "date",
            run_date=datetime.now(UTC) + _GROUP_MESSAGE_LIFETIME,
            id=f"group-message:{chat_id}:{message_id}",
            replace_existing=True,
            misfire_grace_time=None,
            kwargs={"chat_id": chat_id, "message_id": message_id},
        )

    async def _delete_group_message(self, chat_id: int, message_id: int) -> None:
        try:
            try:
                await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError as exc:
            log.debug(
                "群消息删除失败，聊天 %s，消息 %s（%s）",
                chat_id,
                message_id,
                type(exc).__name__,
            )

    def cancel(self, giveaway_id: int) -> None:
        job_id = f"giveaway-{giveaway_id}"
        try:
            self._scheduler.remove_job(job_id)
            log.info("canceled %s", job_id)
        except Exception:
            log.debug("no job to cancel: %s", job_id)
