"""将同一场抽奖发布到所有必加频道/群组，并记录每处消息。"""

from __future__ import annotations

import asyncio
import logging
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
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


@dataclass(frozen=True)
class PublicationResult:
    giveaway_id: int
    sent: int
    failed: tuple[str, ...]
    canceled: bool = False


def _expired(gw: Giveaway) -> bool:
    return gw.draw_mode != "count" and gw.end_at <= datetime.now(UTC)


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
                text = render(
                    "giveaway_running",
                    "zh",
                    id=gid,
                    title=gw.title,
                    description=gw.description,
                    prizes=gw.prize_tiers,
                    channels=[linked_requirement(x) for x in gw.required_channels],
                    groups=[linked_requirement(x) for x in gw.required_groups],
                    ends_at=format_draw_condition(
                        gw.end_at, gw.draw_mode, gw.target_participants, get_settings().tz
                    ),
                    count=count,
                    commit=gw.draw_commit,
                )
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
                await session.commit()
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
