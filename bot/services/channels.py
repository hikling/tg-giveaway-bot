"""频道与群组的真实成员校验；无法确认成员资格时拒绝报名。"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

log = logging.getLogger(__name__)


def _normalise(raw: str) -> str | int:
    ref = raw.split("|", 1)[0].strip()
    if re.fullmatch(r"-[1-9][0-9]{0,18}", ref) and -(2**63) <= int(ref):
        return int(ref)
    if ref.startswith(("https://t.me/", "http://t.me/", "t.me/")):
        path = urlparse(ref if "://" in ref else "https://" + ref).path.strip("/")
        ref = path
    ref = ref.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", ref) or ref == "joinchat":
        raise ValueError("请输入公开用户名，或私有频道/群组的负数编号；邀请链接需附在编号后面")
    return "@" + ref


def parse_requirements(text: str) -> list[str]:
    if not text.strip() or text.strip() == "-":
        return []
    result = []
    # 每行一个，兼容旧版以空格和逗号分隔公开用户名的格式。
    for line in text.replace("，", ",").splitlines():
        entries = [line] if "|" in line else re.split(r"[\s,]+", line)
        for entry in entries:
            if not entry.strip():
                continue
            ref = _normalise(entry)
            parts = entry.split("|", 1)
            link = parts[1].strip() if len(parts) == 2 else ""
            if len(parts) == 2:
                url = urlparse(link)
                if url.scheme != "https" or url.netloc != "t.me" or not url.path.strip("/"):
                    raise ValueError("邀请链接需为 https://t.me/ 开头的有效链接")
            value = str(ref) + ("|" + link if link else "")
            if value not in result:
                result.append(value)
    return result


def display_requirement(raw: str) -> str:
    parts = raw.split("|", 1)
    return parts[0] + (f"（{parts[1]}）" if len(parts) == 2 else "")


async def missing_channels(bot: Bot, user_id: int, channels: Sequence[str]) -> tuple[str, ...]:
    missing = []
    for raw in channels:
        try:
            ref = _normalise(raw)
            member = await bot.get_chat_member(ref, user_id)
        except (TelegramAPIError, ValueError) as exc:
            log.warning("成员校验失败：%s：%s", raw, exc)
            missing.append(display_requirement(raw))
            continue
        status = str(getattr(member.status, "value", member.status) or "")
        if status in {"member", "administrator", "creator", "owner"}:
            continue
        if status == "restricted" and getattr(member, "is_member", False):
            continue
        missing.append(display_requirement(raw))
    return tuple(missing)


async def validate_channels(
    bot: Bot, channels: Sequence[str], *, expected_type: str | None = None
) -> tuple[str, ...]:
    problems = []
    if not channels:
        return ()
    me = await bot.get_me()
    for raw in channels:
        try:
            ref = _normalise(raw)
            if expected_type:
                chat = await bot.get_chat(ref)
                kind = str(getattr(chat.type, "value", chat.type))
                allowed = {"channel"} if expected_type == "channel" else {"group", "supergroup"}
                if kind not in allowed:
                    problems.append(
                        f"{raw}（类型不符，请填写{'频道' if expected_type == 'channel' else '群组'}）"
                    )
                    continue
            member = await bot.get_chat_member(ref, me.id)
        except (TelegramAPIError, ValueError):
            problems.append(f"{raw}（无法访问，请检查编号并添加机器人）")
            continue
        status = str(getattr(member.status, "value", member.status) or "")
        if status not in {"administrator", "creator", "owner"}:
            problems.append(f"{raw}（机器人必须是管理员）")
    return tuple(problems)
