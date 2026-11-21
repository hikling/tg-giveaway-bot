"""在调用方持有的抽奖写事务内出题和判题，答对与报名一起提交。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import PoetryAttempt, PoetryRotation
from bot.db.repo import add_participant
from bot.poetry import POEMS

MAX_WRONG = 5
_rng = secrets.SystemRandom()


@dataclass(frozen=True)
class Challenge:
    giveaway_id: int
    question_id: int
    options: tuple[str, ...]
    nonce: str
    remaining: int


def view(row: PoetryAttempt) -> Challenge:
    return Challenge(
        row.giveaway_id,
        row.question_id,
        tuple(row.options),
        row.nonce or "",
        MAX_WRONG - row.wrong_count,
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
    await session.flush()


async def get_challenge(session: AsyncSession, gid: int, uid: int) -> Challenge | None:
    row = await session.get(PoetryAttempt, (gid, uid))
    if row is None:
        row = PoetryAttempt(giveaway_id=gid, user_id=uid, wrong_count=0)
        session.add(row)
        # _next queries the rotation, so suppress autoflush of the unfinished row.
        with session.no_autoflush:
            await _next(session, row)
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
    if row.options[choice] == POEMS[row.question_id].answer:
        row.nonce = None
        created = await add_participant(
            session, giveaway_id=gid, user_id=uid, username=username, verified=True
        )
        return ("ok" if created else "already"), None
    row.wrong_count += 1
    row.nonce = None
    if row.wrong_count >= MAX_WRONG:
        return "locked", None
    await _next(session, row)
    return "wrong", view(row)
