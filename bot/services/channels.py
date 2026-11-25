"""成员校验、命名邀请链接与发布目标解析。"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from markupsafe import Markup

from bot.utils.esc import Raw

log = logging.getLogger(__name__)


def _normalise(raw: str) -> str | int:
    ref = raw.split("|", 1)[0].strip()
    if re.fullmatch(r"-[1-9][0-9]{0,18}", ref) and -(2**63) <= int(ref):
        return int(ref)
    if ref.startswith(("https://t.me/", "http://t.me/", "t.me/")):
        ref = urlparse(ref if "://" in ref else "https://" + ref).path.strip("/")
    ref = ref.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", ref) or ref == "joinchat":
        raise ValueError("私有邀请链接需绑定频道或群组的负数编号")
    return "@" + ref


def _valid_url(link: str) -> bool:
    url = urlparse(link)
    return (
        url.scheme == "https"
        and url.netloc == "t.me"
        and bool(url.path.strip("/"))
        and not any(c in link for c in '\r\n\t<>"')
    )


def _parse_entry(entry: str) -> str:
    parts = [part.strip() for part in entry.split("|")]
    if len(parts) == 1:
        return str(_normalise(parts[0]))
    if len(parts) not in {2, 3} or not _valid_url(parts[1]):
        raise ValueError("请填写 名称 | 邀请链接 | 编号（公开链接可以不填编号）")
    link = parts[1]
    # 兼容旧版及已入库的 编号|链接|名称。
    if parts[0].startswith(("@", "-", "https://", "t.me/")):
        ref, name = _normalise(parts[0]), parts[2] if len(parts) == 3 else ""
    else:
        name = parts[0]
        ref = _normalise(parts[2] if len(parts) == 3 else link)
    if len(name) > 100 or "\n" in name or (len(parts) == 3 and not name):
        raise ValueError("显示名称需为1至100个字")
    return str(ref) + "|" + link + ("|" + name if name else "")


def parse_requirements(text: str) -> list[str]:
    if not text.strip() or text.strip() == "-":
        return []
    result = []
    for line in text.splitlines():
        entries = [line] if "|" in line else re.split(r"[\s,，]+", line)
        for entry in entries:
            if not entry.strip():
                continue
            value = _parse_entry(entry)
            if value not in result:
                result.append(value)
    return result


def display_requirement(raw: str) -> str:
    parts = raw.split("|")
    return parts[2] if len(parts) == 3 else parts[0]


def format_requirement_line(raw: str) -> str:
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) == 3:
        ref, link, name = parts
        if ref.startswith("-"):
            return f"{name} | {link} | {ref}"
        return f"{name} | {link}"
    if len(parts) == 2:
        return f"{parts[0]} | {parts[1]}"
    return parts[0]


def requirement_url(raw: str) -> str | None:
    parts = raw.split("|")
    if len(parts) > 1 and _valid_url(parts[1]):
        return parts[1]
    ref = _normalise(raw)
    return f"https://t.me/{ref[1:]}" if isinstance(ref, str) else None


def linked_requirement(raw: str) -> Markup:
    name, url = display_requirement(raw), requirement_url(raw)
    if url:
        return Markup(f'<a href="{html.escape(url, quote=True)}">{html.escape(name)}</a>')
    return Markup(html.escape(name))


def linked_requirements(values: Sequence[str]) -> Raw:
    return Raw("\n".join(str(linked_requirement(raw)) for raw in values) or "无")


async def missing_channels(bot: Bot, user_id: int, channels: Sequence[str]) -> tuple[str, ...]:
    """保留原始条目，调用方可分别生成名称提示和命名链接。"""
    missing = []
    for raw in channels:
        try:
            member = await bot.get_chat_member(_normalise(raw), user_id)
        except (TelegramAPIError, ValueError) as exc:
            log.warning("成员校验失败：%s：%s", display_requirement(raw), type(exc).__name__)
            missing.append(raw)
            continue
        status = str(getattr(member.status, "value", member.status) or "")
        if status in {"member", "administrator", "creator", "owner"}:
            continue
        if status == "restricted" and getattr(member, "is_member", False):
            continue
        missing.append(raw)
    return tuple(missing)


async def validate_channels(
    bot: Bot, channels: Sequence[str], *, expected_type: str | None = None
) -> tuple[str, ...]:
    problems = []
    if not channels:
        return ()
    me = await bot.get_me()
    for raw in channels:
        name = display_requirement(raw)
        try:
            ref = _normalise(raw)
            if expected_type:
                chat = await bot.get_chat(ref)
                kind = str(getattr(chat.type, "value", chat.type))
                allowed = {"channel"} if expected_type == "channel" else {"group", "supergroup"}
                if kind not in allowed:
                    problems.append(
                        f"{name}（类型不符，请填写{'频道' if expected_type == 'channel' else '群组'}）"
                    )
                    continue
            member = await bot.get_chat_member(ref, me.id)
        except (TelegramAPIError, ValueError):
            problems.append(f"{name}（无法访问，请检查编号并添加机器人）")
            continue
        status = str(getattr(member.status, "value", member.status) or "")
        if status not in {"administrator", "creator", "owner"}:
            problems.append(f"{name}（机器人必须是管理员）")
        elif expected_type == "channel" and not getattr(member, "can_post_messages", True):
            problems.append(f"{name}（机器人缺少发布消息权限）")
    return tuple(problems)


@dataclass(frozen=True)
class Destination:
    chat_id: int
    name: str


async def resolve_destinations(bot: Bot, entries: Sequence[str]) -> list[Destination]:
    result = {}
    for raw in entries:
        chat = await bot.get_chat(_normalise(raw))
        # 公开链接可核对真实目标，避免名称、链接和校验编号指向不同群。
        link = requirement_url(raw)
        if link:
            try:
                link_ref = _normalise(link)
            except ValueError:
                link_ref = None  # 私有邀请只能由管理员提供编号进行绑定。
            if link_ref is not None and link_ref != _normalise(raw):
                linked_chat = await bot.get_chat(link_ref)
                if linked_chat.id != chat.id:
                    raise ValueError("邀请链接和校验编号不属于同一频道或群组")
        if chat.id not in result:
            result[chat.id] = Destination(chat.id, display_requirement(raw))
    return list(result.values())


async def resolve_publication_targets(
    bot: Bot, channels: Sequence[str], groups: Sequence[str]
) -> list[Destination]:
    problems = [
        *await validate_channels(bot, channels, expected_type="channel"),
        *await validate_channels(bot, groups, expected_type="group"),
    ]
    if problems:
        raise ValueError("\n".join(problems))
    return await resolve_destinations(bot, [*channels, *groups])
