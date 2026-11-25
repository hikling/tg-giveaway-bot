"""将同一场抽奖发布到所有必加频道/群组，并记录每处消息。"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from sqlalchemy import select

from bot.db.base import get_sessionmaker
from bot.db.models import Giveaway, GiveawayPost, GiveawayStatus
from bot.db.repo import claim_giveaway, count_participants
from bot.keyboards.participate import participate_keyboard
from bot.services.channels import linked_requirement
from bot.settings import get_settings
from bot.utils.render import render
from bot.utils.timeparse import format_draw_condition

log = logging.getLogger(__name__)
# 项目按单实例部署；串行发布/补发避免重复发送同一处待发消息。
_publish_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# 记录每个已发抽奖帖最近一次展示的报名人数，避免人数未变化时重复调用编辑接口。
_last_displayed_counts: dict[tuple[str, int, int, int], int] = {}
_SYNC_MIN_INTERVAL = 2.0
_last_sync_at: dict[int, float] = {}
_pending_sync_tasks: dict[int, asyncio.Task[None]] = {}
_sync_dirty: set[int] = set()


@dataclass(frozen=True)
class PublicationResult:
    giveaway_id: int
    sent: int
    failed: tuple[str, ...]
    canceled: bool = False


def _expired(gw: Giveaway) -> bool:
    return gw.draw_mode != "count" and gw.end_at <= datetime.now(UTC)


def render_running_post(gw: Giveaway, count: int) -> str:
    return render(
        "giveaway_running",
        "zh",
        id=gw.id,
        title=gw.title,
        description=gw.description,
        prizes=gw.prize_tiers,
        channels=[linked_requirement(x) for x in gw.required_channels or ()],
        groups=[linked_requirement(x) for x in gw.required_groups or ()],
        ends_at=format_draw_condition(
            gw.end_at, gw.draw_mode, gw.target_participants, get_settings().tz
        ),
        count=count,
        commit=gw.draw_commit,
    )


async def post_locations(
    gid: int, legacy_chat: int | None = None, legacy_message: int | None = None
) -> list[tuple[int, int]]:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(GiveawayPost).where(
                        GiveawayPost.giveaway_id == gid, GiveawayPost.message_id.is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        result = {p.chat_id: p.message_id for p in rows}
    if legacy_chat and legacy_message:
        result.setdefault(legacy_chat, legacy_message)
    return list(result.items())


async def publish_all(bot: Bot, gid: int) -> PublicationResult:
    """已成功记录的位置不会重发；全部失败则取消抽奖，部分失败可显式补发。"""
    loop = asyncio.get_running_loop()
    lock = _publish_locks.setdefault(loop, asyncio.Lock())
    async with lock:
        async with get_sessionmaker()() as session:
            gw = await claim_giveaway(session, gid, GiveawayStatus.running)
            if gw is None:
                raise ValueError("抽奖已结束、取消或不存在，不能补发抽奖帖")
            rows = (
                (
                    await session.execute(
                        select(GiveawayPost)
                        .where(GiveawayPost.giveaway_id == gid)
                        .order_by(GiveawayPost.chat_id)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                raise ValueError("该抽奖没有需要补发的自动发布记录")
            gw.publishing = True
            pending = [(p.chat_id, p.label) for p in rows if p.message_id is None]
            await session.commit()
        for chat_id, _ in pending:
            async with get_sessionmaker()() as session:
                gw = await session.get(Giveaway, gid)
                if gw.status != GiveawayStatus.running or _expired(gw):
                    break
                count = await count_participants(session, gid, exclude_blacklisted=True)
                text = render_running_post(gw, count)
            try:
                sent = await bot.send_message(
                    chat_id,
                    text,
                    reply_markup=participate_keyboard(gid, "zh"),
                    link_preview_options={"is_disabled": True},
                )
            except (TelegramAPIError, TimeoutError) as exc:
                async with get_sessionmaker()() as session:
                    row = await session.get(GiveawayPost, (gid, chat_id))
                    row.send_error = type(exc).__name__
                    await session.commit()
                log.warning("抽奖%s发布失败，目标%s：%s", gid, chat_id, type(exc).__name__)
                continue
            async with get_sessionmaker()() as session:
                row = await session.get(GiveawayPost, (gid, chat_id))
                row.message_id = sent.message_id
                row.send_error = None
                gw = await session.get(Giveaway, gid)
                if gw.chat_id is None:
                    # 保留旧版单目标字段，兼容旧公告与数据备份。
                    gw.chat_id, gw.message_id = chat_id, sent.message_id
                commit_key = gw.draw_commit or ""
                await session.commit()
            _last_displayed_counts[(commit_key, gid, chat_id, sent.message_id)] = count
        async with get_sessionmaker()() as session:
            gw = await claim_giveaway(session, gid, GiveawayStatus.running)
            rows = (
                (await session.execute(select(GiveawayPost).where(GiveawayPost.giveaway_id == gid)))
                .scalars()
                .all()
            )
            count = sum(p.message_id is not None for p in rows)
            failed = tuple(p.label for p in rows if p.message_id is None)
            if gw is None:
                return PublicationResult(gid, count, failed)
            gw.publishing = False
            if count == 0:
                gw.status = GiveawayStatus.canceled
            overdue = _expired(gw)
            await session.commit()
        if count and overdue:
            from bot.services.giveaway import finalize_and_publish

            await finalize_and_publish(gid, bot)
        return PublicationResult(gid, count, failed, canceled=count == 0)


async def _edit_post_count(
    bot: Bot,
    *,
    gid: int,
    chat_id: int,
    message_id: int,
    text: str,
    markup,
    cache_key: tuple[str, int, int, int],
    count: int,
) -> bool:
    for attempt in range(2):
        try:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=markup,
                link_preview_options={"is_disabled": True},
            )
            _last_displayed_counts[cache_key] = count
            return True
        except TelegramRetryAfter as exc:
            if attempt == 0:
                await asyncio.sleep(exc.retry_after)
                continue
            log.warning("刷新抽奖%s人数失败（TelegramRetryAfter）", gid)
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                _last_displayed_counts[cache_key] = count
            else:
                log.warning("刷新抽奖%s人数失败：%s", gid, exc)
            return False
        except (TelegramAPIError, TimeoutError) as exc:
            log.warning("刷新抽奖%s人数失败（%s）", gid, type(exc).__name__)
            return False
    return False


async def refresh_giveaway_post_count(
    bot: Bot, gid: int, *, allow_finished: bool = False
) -> int:
    """刷新单场抽奖全部已发帖子的报名人数。"""
    if not hasattr(bot, "edit_message_text"):
        return 0
    async with get_sessionmaker()() as session:
        gw = await session.get(Giveaway, gid)
        if gw is None or gw.publishing:
            return 0
        if allow_finished:
            if gw.status != GiveawayStatus.finished:
                return 0
        elif gw.status != GiveawayStatus.running or _expired(gw):
            return 0
        count = await count_participants(session, gid, exclude_blacklisted=True)
        text = render_running_post(gw, count)
        legacy_chat, legacy_msg = gw.chat_id, gw.message_id
        commit_key = gw.draw_commit or ""

    locations = await post_locations(gid, legacy_chat, legacy_msg)
    targets = [
        (chat_id, message_id)
        for chat_id, message_id in locations
        if _last_displayed_counts.get((commit_key, gid, chat_id, message_id)) != count
    ]
    if not targets:
        return 0

    markup = participate_keyboard(gid, "zh")
    updated = 0
    for chat_id, message_id in targets:
        if await _edit_post_count(
            bot,
            gid=gid,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            markup=markup,
            cache_key=(commit_key, gid, chat_id, message_id),
            count=count,
        ):
            updated += 1
    return updated


async def refresh_running_post_counts(bot: Bot) -> int:
    """定时刷新进行中抽奖帖的已报名人数，避免帖子长期停留在 0 人。"""
    if not hasattr(bot, "edit_message_text"):
        return 0
    async with get_sessionmaker()() as session:
        running_ids = list(
            await session.scalars(
                select(Giveaway.id)
                .where(Giveaway.status == GiveawayStatus.running, Giveaway.publishing.is_(False))
                .order_by(Giveaway.id)
            )
        )
    active_ids = set(running_ids)
    for key in list(_last_displayed_counts):
        if key[1] not in active_ids:
            _last_displayed_counts.pop(key, None)
    for gid in list(_last_sync_at):
        if gid not in active_ids:
            _last_sync_at.pop(gid, None)

    updated = 0
    for gid in running_ids:
        updated += await refresh_giveaway_post_count(bot, gid)
    return updated


async def _sync_worker(bot: Bot, gid: int) -> None:
    try:
        while gid in _sync_dirty:
            _sync_dirty.discard(gid)
            elapsed = time.monotonic() - _last_sync_at.get(gid, 0.0)
            if elapsed < _SYNC_MIN_INTERVAL:
                await asyncio.sleep(_SYNC_MIN_INTERVAL - elapsed)
                _sync_dirty.discard(gid)
            _last_sync_at[gid] = time.monotonic()
            await refresh_giveaway_post_count(bot, gid)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("实时刷新抽奖%s人数失败", gid)
    finally:
        current = asyncio.current_task()
        if _pending_sync_tasks.get(gid) is current:
            _pending_sync_tasks.pop(gid, None)


def schedule_post_count_sync(bot: Bot, gid: int) -> None:
    """报名人数变化后节流触发实时刷新，兼顾即时更新与防限流。"""
    if not hasattr(bot, "edit_message_text"):
        return
    _sync_dirty.add(gid)
    existing = _pending_sync_tasks.get(gid)
    if existing is not None and not existing.done():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _pending_sync_tasks[gid] = asyncio.create_task(_sync_worker(bot, gid))


async def schedule_running_post_counts_sync(bot: Bot) -> None:
    if not hasattr(bot, "edit_message_text"):
        return
    async with get_sessionmaker()() as session:
        running_ids = list(
            await session.scalars(
                select(Giveaway.id).where(
                    Giveaway.status == GiveawayStatus.running, Giveaway.publishing.is_(False)
                )
            )
        )
    for gid in running_ids:
        schedule_post_count_sync(bot, gid)


def cancel_post_count_sync(gid: int) -> None:
    _sync_dirty.discard(gid)
    task = _pending_sync_tasks.pop(gid, None)
    if task is not None and not task.done():
        task.cancel()


async def flush_post_count_sync(gid: int | None = None) -> None:
    tasks = (
        [_pending_sync_tasks[gid]]
        if gid is not None and gid in _pending_sync_tasks
        else list(_pending_sync_tasks.values())
    )
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
