from __future__ import annotations

import logging
import re
from typing import Literal

from aiogram.exceptions import TelegramAPIError
from aiogram.types import MenuButtonDefault
from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from bot.db.base import get_sessionmaker
from bot.db.models import (
    Blacklist,
    Giveaway,
    GiveawayPost,
    GiveawayStatus,
    Participant,
    PrizeClaim,
)
from bot.db.prize_repo import stock_by_tier
from bot.db.repo import (
    add_admin,
    ban_user,
    count_participants,
    delete_participations,
    get_giveaway,
    get_user_names,
    is_admin_db,
    list_admins,
    list_winners,
    pending_notifications,
    remove_admin,
    unban_user,
)
from bot.services import giveaway as giveaway_service
from bot.services import prize as prize_service
from bot.services.admin_menu import set_admin_menu
from bot.services.channels import display_requirement, format_requirement_line, requirement_url
from bot.services.notifier import retry_pending
from bot.services.publication import publish_all
from bot.settings import Settings
from bot.utils.prize_input import parse_input
from bot.utils.timeparse import format_draw_condition
from bot.web.admin_common import AdminError, read_payload, validate_prize_content

log = logging.getLogger(__name__)
_PAGE_SIZE = 25


class GiveawayAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["cancel", "reroll", "retry", "republish", "stock-preview", "add-stock"]
    tier: str = Field(default="", max_length=64)
    codes: str = Field(default="", max_length=65_536)


class AccessAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["ban", "unban", "add-admin", "remove-admin"]
    user_id: str = Field(max_length=19)
    reason: str = Field(default="", max_length=512)


def _id(value: str, message: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 2**63 - 1:
        raise AdminError(message)
    return int(value)


def _summary(gw: Giveaway, participants: int, tz: str) -> dict:
    draw_mode = gw.draw_mode or "time"
    tiers = gw.prize_tiers or []
    return {
        "id": gw.id,
        "title": gw.title,
        "status": gw.status.value,
        "condition": format_draw_condition(gw.end_at, gw.draw_mode, gw.target_participants, tz),
        "participants": participants,
        "publishing": gw.publishing,
        "draw_mode": draw_mode,
        "target_participants": gw.target_participants,
        "end_at": gw.end_at.isoformat() if draw_mode != "count" and gw.end_at else None,
        "prize_mode": gw.prize_mode or "none",
        "tier_count": len(tiers),
        "winner_slots": sum(int(item.get("count", 0) or 0) for item in tiers),
        "created_at": gw.created_at.isoformat() if gw.created_at else None,
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
    query = request.query.get("q", "").strip()[:256]
    if query:
        id_text = query.removeprefix("#").strip()
        if re.fullmatch(r"[1-9][0-9]{0,18}", id_text) and int(id_text) <= 2**63 - 1:
            stmt = stmt.where(
                or_(
                    Giveaway.id == int(id_text),
                    Giveaway.title.contains(query, autoescape=True),
                )
            )
        else:
            stmt = stmt.where(Giveaway.title.contains(query, autoescape=True))

    tz = request.app["settings"].tz
    counts = {item.value: 0 for item in GiveawayStatus}
    async with get_sessionmaker()() as session:
        rows = (await session.scalars(stmt)).all()
        items = [
            _summary(gw, await count_participants(session, gw.id), tz) for gw in rows[:_PAGE_SIZE]
        ]
        status_rows = (
            await session.execute(select(Giveaway.status, func.count(Giveaway.id)).group_by(Giveaway.status))
        ).all()
        for row_status, total in status_rows:
            counts[row_status.value] = int(total or 0)
    counts["all"] = sum(counts.values())
    next_before = rows[_PAGE_SIZE - 1].id if len(rows) > _PAGE_SIZE else None
    return web.json_response({"items": items, "next_before": next_before, "tz": tz, "counts": counts})


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
            post.chat_id: {
                "name": post.label,
                "sent": post.message_id is not None,
                "error": post.send_error,
            }
            for post in await session.scalars(
                select(GiveawayPost)
                .where(GiveawayPost.giveaway_id == gid)
                .order_by(GiveawayPost.chat_id)
            )
        }
        claim_rows = (
            await session.execute(
                select(PrizeClaim.user_id, PrizeClaim.tier_name).where(
                    PrizeClaim.giveaway_id == gid
                )
            )
        ).all()
        claimed_pairs = {(uid, tier) for uid, tier in claim_rows}
        claimed_tiers = {tier for _, tier in claim_rows}
        winner_rows = (
            await list_winners(session, gid) if gw.status == GiveawayStatus.finished else []
        )
        winner_names = await get_user_names(session, [row.user_id for row in winner_rows])
    if gw.chat_id and gw.message_id:
        posts.setdefault(gw.chat_id, {"name": f"聊天 {gw.chat_id}", "sent": True, "error": None})

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
                "rerollable": gw.status == GiveawayStatus.finished and name not in claimed_tiers,
            }
        )
    winners = [
        {
            "user_id": str(row.user_id),
            "username": winner_names.get(row.user_id),
            "tier": row.tier_name,
            "notified_ok": row.notified_ok,
            "notified_at": row.notified_at.isoformat() if row.notified_at else None,
            "notify_error": row.notify_error,
            "claimed": (row.user_id, row.tier_name) in claimed_pairs,
        }
        for row in winner_rows
    ]
    return web.json_response(
        {
            **_summary(gw, len(ids), settings.tz),
            "description": gw.description,
            "prize_mode": gw.prize_mode,
            "tiers": tiers,
            "channels": [display_requirement(raw) for raw in gw.required_channels or []],
            "groups": [display_requirement(raw) for raw in gw.required_groups or []],
            "requirements": {
                "channels": [
                    {"name": display_requirement(raw), "url": requirement_url(raw)}
                    for raw in gw.required_channels or []
                ],
                "groups": [
                    {"name": display_requirement(raw), "url": requirement_url(raw)}
                    for raw in gw.required_groups or []
                ],
            },
            "posts": list(posts.values()),
            "pending_notifications": len(pending),
            "winners": winners,
            "draw_commit": gw.draw_commit,
            "draw_seed": gw.draw_seed if gw.status == GiveawayStatus.finished else None,
            "gate": {**_gate(settings), "distribution": _distribution(ids, settings.min_user_id)},
        }
    )


async def _stock_action(gw: Giveaway, payload: GiveawayAction, tier: str) -> dict:
    if gw.status == GiveawayStatus.canceled:
        raise AdminError("这场抽奖已取消，无法补充奖池。", status=409)
    if gw.prize_mode not in {"shared", "unique"}:
        raise AdminError("这场抽奖由主办方发奖，无需补充奖池。")
    codes = parse_input(payload.codes, gw.prize_mode)
    if not codes:
        raise AdminError("请填写奖品内容。")
    validate_prize_content(codes)
    if payload.action == "stock-preview":
        count = await prize_service.new_stock_count(gw.id, gw.prize_mode, codes)
        if not count:
            raise AdminError("这些独立码已在本场奖池中，没有新奖品可入库。")
        unit = "份共享文本" if gw.prize_mode == "shared" else "条独立码"
        message = f"识别到 {count} {unit}。"
        if count < len(codes):
            message += f" 已跳过 {len(codes) - count} 条重复独立码。"
        return {"message": message, "count": count}
    count = await prize_service.add_codes(
        gw.id, tier, codes, max_claims=prize_service.max_claims_for(gw.prize_mode)
    )
    message = f"已为「{tier}」补充 {count} 份奖品。"
    if count < len(codes):
        message += f" 已跳过 {len(codes) - count} 条重复独立码。"
    return {"message": message, "count": count}


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
                "claimed": "这个奖项已有奖品被领取，无法重抽。",
            }.get(str(exc), "这场抽奖目前无法重抽。")
            raise AdminError(message, status=409) from exc
        count = len(picks)
        message = f"「{tier}」重抽出 {count} 名中奖者。"
        if shortfall is not None:
            message += f"计划 {shortfall.requested} 名，符合条件的人数不足。"
        return web.json_response({"message": message, "count": count})
    return web.json_response(await _stock_action(gw, payload, tier))


async def location_options(_request: web.Request) -> web.Response:
    channels: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(Giveaway.required_channels, Giveaway.required_groups)
                .order_by(Giveaway.id.desc())
                .limit(80)
            )
        ).all()
    for ch_list, gp_list in rows:
        for raw in ch_list or []:
            if raw not in channels and len(channels) < 24:
                channels[raw] = {
                    "name": display_requirement(raw),
                    "line": format_requirement_line(raw),
                    "private": raw.startswith("-"),
                }
        for raw in gp_list or []:
            if raw not in groups and len(groups) < 24:
                groups[raw] = {
                    "name": display_requirement(raw),
                    "line": format_requirement_line(raw),
                    "private": raw.startswith("-"),
                }
    return web.json_response(
        {"channels": list(channels.values()), "groups": list(groups.values())}
    )


async def access_details(request: web.Request) -> web.Response:
    settings = request.app["settings"]
    configured = set(settings.admin_ids)
    async with get_sessionmaker()() as session:
        stored = {row.user_id for row in await list_admins(session)}
        banned = (
            await session.scalars(select(Blacklist).order_by(Blacklist.banned_at.desc()))
        ).all()
    return web.json_response(
        {
            "admins": [
                {"id": str(uid), "configured": uid in configured}
                for uid in sorted(configured | stored)
            ],
            "blacklist": [
                {
                    "user_id": str(row.user_id),
                    "reason": row.reason or "",
                    "banned_at": row.banned_at.isoformat() if row.banned_at else None,
                }
                for row in banned
            ],
            "current_admin_id": str(request["admin_id"]),
            "gate": _gate(settings),
        }
    )


async def access_action(request: web.Request) -> web.Response:
    payload = await read_payload(request, AccessAction)
    uid = _id(payload.user_id.strip(), "请填写有效的 Telegram 用户编号。")
    actor = request["admin_id"]
    settings = request.app["settings"]
    if payload.action == "remove-admin":
        if uid in settings.admin_ids:
            raise AdminError("部署配置中的管理员无法在小程序移除。")
        if uid == actor:
            raise AdminError("不能移除当前正在使用的管理员账号。")
    async with get_sessionmaker()() as session:
        if payload.action == "ban":
            await ban_user(session, uid, reason=payload.reason.strip() or None, banned_by=actor)
            removed = await delete_participations(session, uid)
            message = f"已将 {uid} 加入黑名单，清除 {removed} 条报名记录。"
        elif payload.action == "unban":
            await unban_user(session, uid)
            message = f"已将 {uid} 移出黑名单。"
        elif payload.action == "remove-admin":
            if not await is_admin_db(session, uid):
                raise AdminError("该用户不在动态管理员列表中。", status=404)
            await remove_admin(session, uid)
            message = f"已移除管理员 {uid}。"
        else:
            await add_admin(session, uid, added_by=actor)
            message = f"已添加管理员 {uid}。"
        await session.commit()
    if payload.action == "add-admin":
        await set_admin_menu(request.app["bot"], settings, uid)
    elif payload.action == "remove-admin":
        try:
            await request.app["bot"].set_chat_menu_button(
                chat_id=uid, menu_button=MenuButtonDefault()
            )
        except TelegramAPIError as exc:
            log.warning("管理员 %s 恢复默认菜单失败（%s）", uid, type(exc).__name__)
    return web.json_response({"message": message})
