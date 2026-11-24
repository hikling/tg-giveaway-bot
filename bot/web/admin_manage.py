from __future__ import annotations

import asyncio
import re
import weakref
from typing import Literal

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from bot.db.base import get_sessionmaker
from bot.db.models import Giveaway, GiveawayPost, GiveawayStatus, Participant
from bot.db.prize_repo import stock_by_tier
from bot.db.repo import (
    add_admin,
    ban_user,
    count_participants,
    delete_participations,
    get_giveaway,
    list_admins,
    pending_notifications,
    unban_user,
)
from bot.services import giveaway as giveaway_service
from bot.services import prize as prize_service
from bot.services.admin_menu import set_admin_menu
from bot.services.channels import display_requirement
from bot.services.notifier import retry_pending
from bot.services.publication import publish_all
from bot.settings import Settings
from bot.utils.prize_input import parse_input
from bot.utils.timeparse import format_draw_condition
from bot.web.admin_common import AdminError, read_payload, validate_prize_content

_PAGE_SIZE = 25
_retry_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()


class GiveawayAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["cancel", "reroll", "retry", "republish", "stock-preview", "add-stock"]
    tier: str = Field(default="", max_length=64)
    codes: str = Field(default="", max_length=65_536)


class AccessAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["ban", "unban", "add-admin"]
    user_id: str = Field(max_length=19)
    reason: str = Field(default="", max_length=512)


def _id(value: str, message: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 2**63 - 1:
        raise AdminError(message)
    return int(value)


def _summary(gw: Giveaway, participants: int, tz: str) -> dict:
    return {
        "id": gw.id,
        "title": gw.title,
        "status": gw.status.value,
        "condition": format_draw_condition(gw.end_at, gw.draw_mode, gw.target_participants, tz),
        "participants": participants,
        "publishing": gw.publishing,
    }


def _gate(settings: Settings) -> dict:
    return {"enabled": settings.account_age_gate, "min_user_id": settings.min_user_id}


def _distribution(ids: list[int], min_user_id: int) -> dict | None:
    if not ids:
        return None
    return {
        "count": len(ids),
        "low": str(ids[0]),
        "median": str(ids[len(ids) // 2]),
        "high": str(ids[-1]),
        "below": sum(uid < min_user_id for uid in ids),
    }


async def giveaway_index(request: web.Request) -> web.Response:
    status = request.query.get("status", "running")
    if status not in {"all", *(status.value for status in GiveawayStatus)}:
        raise AdminError("请选择有效的抽奖状态。")
    stmt = select(Giveaway).order_by(Giveaway.id.desc()).limit(_PAGE_SIZE + 1)
    if status != "all":
        stmt = stmt.where(Giveaway.status == GiveawayStatus(status))
    if "before" in request.query:
        stmt = stmt.where(Giveaway.id < _id(request.query["before"], "分页编号无效。"))

    tz = request.app["settings"].tz
    async with get_sessionmaker()() as session:
        rows = (await session.scalars(stmt)).all()
        items = [
            _summary(gw, await count_participants(session, gw.id), tz) for gw in rows[:_PAGE_SIZE]
        ]
    next_before = rows[_PAGE_SIZE - 1].id if len(rows) > _PAGE_SIZE else None
    return web.json_response({"items": items, "next_before": next_before, "tz": tz})


async def giveaway_detail(request: web.Request) -> web.Response:
    gid = _id(request.match_info["id"], "抽奖编号无效。")
    settings = request.app["settings"]
    async with get_sessionmaker()() as session:
        gw = await get_giveaway(session, gid)
        if gw is None:
            raise AdminError("找不到这场抽奖。", status=404)
        ids = list(
            await session.scalars(
                select(Participant.user_id)
                .where(Participant.giveaway_id == gid, Participant.verified.is_(True))
                .order_by(Participant.user_id)
            )
        )
        stock = await stock_by_tier(session, gid)
        pending = await pending_notifications(session, gid)
        posts = {
            post.chat_id: {"name": post.label, "sent": post.message_id is not None}
            for post in await session.scalars(
                select(GiveawayPost)
                .where(GiveawayPost.giveaway_id == gid)
                .order_by(GiveawayPost.chat_id)
            )
        }
    if gw.chat_id and gw.message_id:
        posts.setdefault(gw.chat_id, {"name": f"聊天 {gw.chat_id}", "sent": True})

    tiers = []
    for tier in gw.prize_tiers or []:
        name = str(tier["name"])
        inventory = stock.get(name)
        tiers.append(
            {
                "name": name,
                "count": tier["count"],
                "total": inventory.total if inventory else 0,
                "claimed": inventory.claimed if inventory else 0,
                "remaining": inventory.remaining if inventory else 0,
            }
        )
    return web.json_response(
        {
            **_summary(gw, len(ids), settings.tz),
            "description": gw.description,
            "prize_mode": gw.prize_mode,
            "tiers": tiers,
            "channels": [display_requirement(raw) for raw in gw.required_channels or []],
            "groups": [display_requirement(raw) for raw in gw.required_groups or []],
            "posts": list(posts.values()),
            "pending_notifications": len(pending),
            "gate": {**_gate(settings), "distribution": _distribution(ids, settings.min_user_id)},
        }
    )


async def _stock_action(gw: Giveaway, payload: GiveawayAction, tier: str) -> dict:
    if gw.prize_mode not in {"shared", "unique"}:
        raise AdminError("这场抽奖由主办方发奖，无需补充奖池。")
    codes = parse_input(payload.codes, gw.prize_mode)
    if not codes:
        raise AdminError("请填写奖品内容。")
    validate_prize_content(codes)
    if payload.action == "stock-preview":
        unit = "份共享文本" if gw.prize_mode == "shared" else "条独立码"
        return {"message": f"识别到 {len(codes)} {unit}。", "count": len(codes)}
    count = await prize_service.add_codes(
        gw.id, tier, codes, max_claims=prize_service.max_claims_for(gw.prize_mode)
    )
    return {"message": f"已为「{tier}」补充 {count} 份奖品。", "count": count}


async def giveaway_action(request: web.Request) -> web.Response:
    gid = _id(request.match_info["id"], "抽奖编号无效。")
    payload = await read_payload(request, GiveawayAction)
    async with get_sessionmaker()() as session:
        gw = await get_giveaway(session, gid)
    if gw is None:
        raise AdminError("找不到这场抽奖。", status=404)

    action = payload.action
    bot = request.app["bot"]
    if action == "cancel":
        if not await giveaway_service.cancel(gid):
            raise AdminError("这场抽奖已结束或取消。", status=409)
        request.app["scheduler"].cancel(gid)
        return web.json_response({"message": f"抽奖 #{gid} 已取消。"})
    if action == "retry":
        lock = _retry_locks.setdefault(gid, asyncio.Lock())
        async with lock:
            count = await retry_pending(bot, gid)
        return web.json_response({"message": f"成功补发 {count} 条中奖私信。", "count": count})
    if action == "republish":
        try:
            result = await publish_all(bot, gid)
        except ValueError as exc:
            raise AdminError(str(exc), status=409) from exc
        if result.canceled:
            message = "所有位置都发布失败，这场抽奖已取消。"
        else:
            message = f"当前有 {result.sent} 处已发布。"
            if result.failed:
                message += f"仍未发布 {'、'.join(result.failed)}。"
        return web.json_response({"message": message, "count": result.sent})

    tier = payload.tier.strip()
    if not tier or not any(tier == entry.get("name") for entry in gw.prize_tiers or []):
        raise AdminError("请选择这场抽奖中的有效奖项。")
    if action == "reroll":
        try:
            picks, shortfall = await giveaway_service.reroll_tier(gid, tier, bot)
        except giveaway_service.RerollError as exc:
            message = {
                "running": "抽奖结束后才能重抽。",
                "canceled": "这场抽奖已取消，无法重抽。",
                "no_such_tier": "抽奖中找不到这个奖项。",
            }.get(str(exc), "这场抽奖目前无法重抽。")
            raise AdminError(message, status=409) from exc
        count = len(picks)
        message = f"「{tier}」重抽出 {count} 名中奖者。"
        if shortfall is not None:
            message += f"计划 {shortfall.requested} 名，符合条件的人数不足。"
        return web.json_response({"message": message, "count": count})
    return web.json_response(await _stock_action(gw, payload, tier))


async def access_details(request: web.Request) -> web.Response:
    settings = request.app["settings"]
    configured = set(settings.admin_ids)
    async with get_sessionmaker()() as session:
        stored = {row.user_id for row in await list_admins(session)}
    return web.json_response(
        {
            "admins": [
                {"id": str(uid), "configured": uid in configured}
                for uid in sorted(configured | stored)
            ],
            "gate": _gate(settings),
        }
    )


async def access_action(request: web.Request) -> web.Response:
    payload = await read_payload(request, AccessAction)
    uid = _id(payload.user_id.strip(), "请填写有效的 Telegram 用户编号。")
    actor = request["admin_id"]
    async with get_sessionmaker()() as session:
        if payload.action == "ban":
            await ban_user(session, uid, reason=payload.reason.strip() or None, banned_by=actor)
            removed = await delete_participations(session, uid)
            message = f"已将 {uid} 加入黑名单，清除 {removed} 条报名记录。"
        elif payload.action == "unban":
            await unban_user(session, uid)
            message = f"已将 {uid} 移出黑名单。"
        else:
            await add_admin(session, uid, added_by=actor)
            message = f"已添加管理员 {uid}。"
        await session.commit()
    if payload.action == "add-admin":
        await set_admin_menu(request.app["bot"], request.app["settings"], uid)
    return web.json_response({"message": message})
