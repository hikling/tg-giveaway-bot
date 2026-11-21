from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_RE_DURATION = re.compile(r"^\s*(\d+)\s*([smhdwSMHDW])\s*$")
_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}

_ABSOLUTE_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
)


def parse_end_at(text: str, tz_name: str = "UTC") -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None

    m = _RE_DURATION.match(text)
    if m:
        n = int(m.group(1))
        if n <= 0:
            return None
        unit = _UNITS[m.group(2).lower()]
        return datetime.now(timezone.utc) + timedelta(**{unit: n})

    for fmt in _ABSOLUTE_FORMATS:
        try:
            local = datetime.strptime(text, fmt)
        except ValueError:
            continue
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        return local.replace(tzinfo=tz).astimezone(timezone.utc)

    return None


def format_local(dt: datetime, tz_name: str = "UTC") -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def parse_prizes(text: str) -> list[dict] | None:
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        sep = ":" if ":" in line else ("：" if "：" in line else None)
        if sep is None:
            return None
        name, _, count_str = line.partition(sep)
        name = name.strip()
        count_str = count_str.strip()
        if not name or not count_str.isdigit():
            return None
        count = int(count_str)
        if count <= 0:
            return None
        out.append({"name": name, "count": count})
    return out or None


def parse_channels(text: str) -> list[str]:
    from bot.services.channels import parse_requirements

    return parse_requirements(text)


# 为兼容旧SQLite表的非空end_at列，纯满人数模式存储一个永不调度的占位时间。
COUNT_ONLY_END = datetime(9999, 1, 1, tzinfo=timezone.utc)


def parse_draw_condition(
    text: str, tz_name: str = "UTC"
) -> tuple[str, datetime, int | None] | None:
    """30m / 满100人 / 30m或满100人（任一先到）均可。"""
    text = text.strip()
    pieces = re.split(r"\s*(?:或者|或|\|)\s*", text)
    mode, deadline, target = "time", None, None
    if len(pieces) > 2:
        return None
    for piece in pieces:
        match = re.fullmatch(r"(?:满\s*)?([0-9]{1,10})\s*人(?:参加)?(?:开奖)?", piece)
        if match:
            if target is not None:
                return None
            target = int(match[1])
            if not 1 <= target <= 1_000_000_000:
                return None
        else:
            if deadline is not None:
                return None
            try:
                deadline = parse_end_at(piece, tz_name)
            except (ValueError, OverflowError):
                return None
            if deadline is None:
                return None
    if deadline is None and target is not None:
        mode, deadline = "count", COUNT_ONLY_END
    elif target is not None:
        mode = "either"
    return mode, deadline, target


def format_draw_condition(
    end_at: datetime, mode: str = "time", target: int | None = None, tz_name: str = "UTC"
) -> str:
    if mode == "count":
        return f"满 {target} 人有效报名后开奖"
    label = format_local(end_at, tz_name)
    if mode == "either":
        return f"{label} 或满 {target} 人有效报名（任一先到）"
    return label
