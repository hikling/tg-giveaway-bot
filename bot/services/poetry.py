"""在调用方持有的抽奖写事务内出题和判题，答对与报名一起提交。"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.callbacks.factories import JoinCb
from bot.db.base import get_sessionmaker
from bot.db.models import Giveaway, GiveawayStatus, PoetryAttempt, PoetryRotation
from bot.db.repo import add_participant, claim_giveaway, has_participated
from bot.i18n import t
from bot.poetry import POEMS

MAX_WRONG = 5
QUESTION_TTL = timedelta(seconds=60)
log = logging.getLogger(__name__)
_rng = secrets.SystemRandom()


@dataclass(frozen=True)
class Challenge:
    giveaway_id: int
    question_id: int
    options: tuple[str, ...]
    nonce: str
    remaining: int
    expires_at: datetime | None = None


def view(row: PoetryAttempt) -> Challenge:
    return Challenge(
        row.giveaway_id,
        row.question_id,
        tuple(row.options),
        row.nonce or "",
        MAX_WRONG - row.wrong_count,
        row.expires_at,
    )


async def _next(session: AsyncSession, row: PoetryAttempt) -> None:
    # SQLite serializes these updates via the giveaway write lock.
    rotation = await session.get(PoetryRotation, row.user_id)
    if rotation is None:
        rotation = PoetryRotation(user_id=row.user_id, used_ids=[])
        session.add(rotation)
    used = list(rotation.used_ids)
    available = [i for i in range(len(POEMS)) if i not in used]
    if not available:
        # 新一轮第一题也不与上一轮最后一题相同。
        available = [i for i in range(len(POEMS)) if i != used[-1]]
        used = []
    qid = _rng.choice(available)
    rotation.used_ids = [*used, qid]
    poem = POEMS[qid]
    options = [poem.answer, *poem.distractors]
    _rng.shuffle(options)
    row.question_id = qid
    row.options = options
    row.nonce = secrets.token_hex(12)
    row.sent_at = None
    row.expires_at = datetime.now(UTC) + QUESTION_TTL
    await session.flush()


async def get_challenge(session: AsyncSession, gid: int, uid: int) -> Challenge | None:
    row = await session.get(PoetryAttempt, (gid, uid))
    if row is None:
        row = PoetryAttempt(giveaway_id=gid, user_id=uid, wrong_count=0)
        session.add(row)
        # _next queries the rotation, so suppress autoflush of the unfinished row.
        with session.no_autoflush:
            await _next(session, row)
    expire_question(row)
    if row.wrong_count >= MAX_WRONG:
        return None
    if not row.nonce:
        # 解封后若管理员已清除了旧报名，重新答题但保留历史错误次数。
        await _next(session, row)
    return view(row)


async def answer(
    session: AsyncSession, gid: int, uid: int, username: str | None, nonce: str, choice: int
) -> tuple[str, Challenge | None]:
    row = await session.get(PoetryAttempt, (gid, uid))
    if row is not None and row.wrong_count >= MAX_WRONG:
        return "locked", None
    if (
        row is None
        or not row.nonce
        or not nonce.isascii()
        or not secrets.compare_digest(row.nonce, nonce)
        or not 0 <= choice < len(row.options)
    ):
        return "stale", None
    if expire_question(row):
        return ("locked" if row.wrong_count >= MAX_WRONG else "timeout"), None
    if row.options[choice] == POEMS[row.question_id].answer:
        row.nonce = None
        row.expires_at = None
        created = await add_participant(
            session, giveaway_id=gid, user_id=uid, username=username, verified=True
        )
        return ("ok" if created else "already"), None
    row.wrong_count += 1
    row.nonce = None
    row.expires_at = None
    if row.wrong_count >= MAX_WRONG:
        return "locked", None
    await _next(session, row)
    return "wrong", view(row)


def expire_question(row: PoetryAttempt, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    if not row.nonce or row.expires_at is None or row.expires_at > now:
        return False
    row.wrong_count = min(MAX_WRONG, row.wrong_count + 1)
    row.nonce = None
    row.expires_at = None
    return True


async def record_delivery(challenge: Challenge, uid: int, *, delivered: bool) -> bool:
    """首次成功送达开始60秒；重发不会延时，已过期或已作答的题目不能复活。"""
    async with get_sessionmaker()() as session:
        gw = await claim_giveaway(
            session, challenge.giveaway_id, GiveawayStatus.running, require_open=True
        )
        if gw is None:
            return False
        row = await session.get(PoetryAttempt, (challenge.giveaway_id, uid))
        if row is None or row.nonce != challenge.nonce:
            return False
        if delivered and expire_question(row):
            await session.commit()
            return False
        if row.sent_at is None:
            if delivered:
                row.sent_at = datetime.now(UTC)
                row.expires_at = row.sent_at + QUESTION_TTL
            else:
                # 明确未送达时暂停本题计时，用户重新点参加后继续同一题。
                row.expires_at = None
        await session.commit()
        return True


async def expire_due_questions(bot: Bot, *, limit: int = 100) -> int:
    """单个周期任务批量扫描，无需为每个用户保留定时器。"""
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        stmt = (
            select(PoetryAttempt.giveaway_id, PoetryAttempt.user_id)
            .join(Giveaway, Giveaway.id == PoetryAttempt.giveaway_id)
            .where(
                PoetryAttempt.nonce.is_not(None),
                PoetryAttempt.expires_at <= now,
                Giveaway.status == GiveawayStatus.running,
                or_(Giveaway.draw_mode == "count", Giveaway.end_at > now),
            )
            .order_by(PoetryAttempt.expires_at)
            .limit(limit)
        )
        keys = (await session.execute(stmt)).all()
    expired = 0
    notices = []
    for gid, uid in keys:
        async with get_sessionmaker()() as session:
            gw = await claim_giveaway(session, gid, GiveawayStatus.running, require_open=True)
            if gw is None:
                continue
            row = await session.get(PoetryAttempt, (gid, uid))
            if row is None or not expire_question(row, now):
                continue
            if not await has_participated(session, gid, uid):
                notices.append((gid, uid, row.wrong_count))
            await session.commit()
            expired += 1
    # 先完成数据库计错，通知失败也不会恢复已失效的机会。
    for gid, uid, wrong in notices:
        locked = wrong >= MAX_WRONG
        markup = (
            None
            if locked
            else InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="继续答题", callback_data=JoinCb(giveaway_id=gid).pack()
                        )
                    ]
                ]
            )
        )
        try:
            await bot.send_message(
                uid,
                t(
                    "poetry.timeout_locked" if locked else "poetry.timeout_notice",
                    id=gid,
                    remaining=MAX_WRONG - wrong,
                ),
                reply_markup=markup,
            )
        except TelegramAPIError:
            log.warning("超时已计错，通知发送失败：抽奖%s 用户%s", gid, uid)
    return expired
