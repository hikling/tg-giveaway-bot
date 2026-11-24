from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from aiohttp import web
from pydantic import UUID4, BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bot.db.base import get_sessionmaker
from bot.db.models import Giveaway, GiveawayCreationRequest, GiveawayPost, GiveawayStatus
from bot.services import giveaway
from bot.services.channels import (
    Destination,
    display_requirement,
    parse_requirements,
    resolve_publication_targets,
)
from bot.services.publication import publish_all
from bot.utils.esc import telegram_text_length
from bot.utils.prize_input import parse_input
from bot.utils.render import render
from bot.utils.timeparse import format_draw_condition, parse_draw_condition
from bot.web.admin_common import AdminError, read_payload, validate_prize_content

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
TierName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
# Preview freezes the deadline in UTC, including across daylight-saving changes.
_FIXED_CONDITION = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z(或满[0-9]+人)?")


class TierInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: TierName
    count: int = Field(ge=1, le=1_000_000_000)
    codes: str = Field(default="", max_length=65_536)


class GiveawayDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    title: Title
    description: str = Field(default="", max_length=4096)
    channels: str = Field(default="", max_length=4096)
    groups: str = Field(default="", max_length=4096)
    condition: str = Field(min_length=1, max_length=120)
    prize_mode: Literal["none", "unique", "shared"]
    tiers: list[TierInput] = Field(min_length=1, max_length=40)


class PublishInput(GiveawayDraftInput):
    request_id: UUID4


@dataclass
class PreparedDraft:
    channels: list[str]
    groups: list[str]
    mode: str
    deadline: datetime
    target: int | None
    tiers: list[dict]
    inventory: list[tuple[str, list[str]]]
    condition: str
    fixed_condition: str
    destinations: list[Destination]


async def _prepare(request: web.Request, draft: GiveawayDraftInput) -> PreparedDraft:
    tz = request.app["settings"].tz
    fixed = _FIXED_CONDITION.fullmatch(draft.condition)
    parsed = (
        parse_draw_condition(fixed[1] + (fixed[2] or ""), "UTC")
        if fixed
        else parse_draw_condition(draft.condition, tz)
    )
    if parsed is None:
        raise AdminError("请填写有效的开奖时间或报名人数。")
    mode, deadline, target = parsed
    deadline = deadline.replace(microsecond=0)
    if mode != "count" and deadline <= datetime.now(UTC):
        raise AdminError("开奖时间必须晚于当前时间。")
    fixed_condition = deadline.strftime("%Y-%m-%d %H:%M:%SZ")
    if mode == "count":
        fixed_condition = f"满{target}人"
    elif mode == "either":
        fixed_condition += f"或满{target}人"
    label = format_draw_condition(deadline, mode, target, tz)

    if len({tier.name for tier in draft.tiers}) != len(draft.tiers):
        raise AdminError("奖项名称不能重复。")
    tiers = [{"name": tier.name, "count": tier.count} for tier in draft.tiers]
    inventory = []
    unique_codes: set[str] = set()
    for tier in draft.tiers:
        codes = [] if draft.prize_mode == "none" else parse_input(tier.codes, draft.prize_mode)
        try:
            validate_prize_content(codes)
        except AdminError as exc:
            raise AdminError(f"「{tier.name}」：{exc}") from exc
        if draft.prize_mode == "unique":
            if unique_codes.intersection(codes):
                raise AdminError(f"「{tier.name}」与其他奖项有重复的独立码，请核对。")
            unique_codes.update(codes)
        inventory.append((tier.name, codes))
    try:
        channels = parse_requirements(draft.channels)
        groups = parse_requirements(draft.groups)
    except ValueError as exc:
        raise AdminError(str(exc)) from exc
    if not (channels or groups):
        raise AdminError("至少填写一个频道或群组。")

    post = render(
        "giveaway_running",
        id=2**63 - 1,
        title=draft.title,
        description=draft.description,
        prizes=tiers,
        channels=[display_requirement(value) for value in channels],
        groups=[display_requirement(value) for value in groups],
        ends_at=label,
        count=0,
        commit="0" * 64,
    )
    if telegram_text_length(post) > 4096:
        raise AdminError("抽奖帖超过 Telegram 的长度限制，请缩短说明或精简奖项。")
    try:
        destinations = await resolve_publication_targets(request.app["bot"], channels, groups)
    except ValueError as exc:
        raise AdminError(str(exc)) from exc
    return PreparedDraft(
        channels,
        groups,
        mode,
        deadline,
        target,
        tiers,
        inventory,
        label,
        fixed_condition,
        destinations,
    )


async def preview(request: web.Request) -> web.Response:
    draft = await read_payload(request, GiveawayDraftInput)
    prepared = await _prepare(request, draft)
    return web.json_response(
        {
            "condition": prepared.condition,
            "fixed_condition": prepared.fixed_condition,
            "destinations": [target.name for target in prepared.destinations],
            "tiers": [
                {**tier, "inventory": len(codes)}
                for tier, (_, codes) in zip(prepared.tiers, prepared.inventory, strict=True)
            ],
        }
    )


async def _existing(creator_id: int, request_id: str) -> Giveaway | None:
    async with get_sessionmaker()() as session:
        record = await session.get(GiveawayCreationRequest, (creator_id, request_id))
        return await session.get(Giveaway, record.giveaway_id) if record else None


async def _publish_result(request: web.Request, gw: Giveaway) -> web.Response:
    if gw.publishing and gw.status == GiveawayStatus.running:
        result = await publish_all(request.app["bot"], gw.id)
        if not result.canceled:
            request.app["scheduler"].schedule(gw.id, gw.end_at, gw.draw_mode)
        sent, failed, canceled = result.sent, result.failed, result.canceled
    else:
        async with get_sessionmaker()() as session:
            posts = (
                await session.scalars(select(GiveawayPost).where(GiveawayPost.giveaway_id == gw.id))
            ).all()
        sent = sum(post.message_id is not None for post in posts)
        failed = [post.label for post in posts if post.message_id is None]
        canceled = gw.status == GiveawayStatus.canceled
    return web.json_response({"id": gw.id, "sent": sent, "failed": failed, "canceled": canceled})


async def create(request: web.Request) -> web.Response:
    draft = await read_payload(request, PublishInput)
    creator_id, request_id = request["admin_id"], str(draft.request_id)
    # The app runs in a single process. The DB key makes retries survive restarts.
    async with request.app["admin_create_lock"]:
        gw = await _existing(creator_id, request_id)
        if gw is None:
            prepared = await _prepare(request, draft)
            try:
                gw = await giveaway.create(
                    creator_id=creator_id,
                    title=draft.title,
                    description=draft.description,
                    prize_tiers=prepared.tiers,
                    required_channels=prepared.channels,
                    required_groups=prepared.groups,
                    draw_mode=prepared.mode,
                    target_participants=prepared.target,
                    end_at=prepared.deadline,
                    turnstile_required=False,
                    prize_mode=draft.prize_mode,
                    publishing=True,
                    destinations=[
                        (target.chat_id, target.name) for target in prepared.destinations
                    ],
                    initial_codes=prepared.inventory,
                    request_id=request_id,
                )
            except IntegrityError:
                gw = await _existing(creator_id, request_id)
                if gw is None:
                    raise
        return await _publish_result(request, gw)
