from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import TypeVar

from aiogram.exceptions import TelegramAPIError
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web
from pydantic import BaseModel, ValidationError

from bot.filters.admin import is_admin

Payload = TypeVar("Payload", bound=BaseModel)
SESSION_LIFETIME = timedelta(hours=8)


class AdminError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


async def read_payload(request: web.Request, schema: type[Payload]) -> Payload:
    if request.content_type != "application/json":
        raise AdminError("请求格式有误。", 415)
    try:
        return schema.model_validate_json(await request.read())
    except ValidationError as exc:
        raise AdminError("填写内容有误，请检查必填项、格式和长度。") from exc


def validate_prize_content(codes: Sequence[str]) -> None:
    # Reserve room for the bot's existing prize delivery message.
    try:
        oversized = any(len(code.encode("utf-16-le")) > 7_800 for code in codes)
    except UnicodeEncodeError as exc:
        raise AdminError("奖品内容包含无效字符。") from exc
    if oversized:
        raise AdminError("每份奖品最多可输入 3900 个字符。")


async def _admin_id(request: web.Request) -> int:
    settings = request.app["settings"]
    scheme, _, init_data = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "tma" or not init_data:
        raise AdminError("请从 Telegram 机器人私聊菜单打开「抽奖管理」。", 401)
    try:
        data = safe_parse_webapp_init_data(settings.bot_token, init_data)
    except ValueError as exc:
        raise AdminError("Telegram 身份验证失败，请关闭后重新打开小程序。", 401) from exc
    age = datetime.now(UTC) - data.auth_date
    if data.user is None or not -timedelta(seconds=60) <= age <= SESSION_LIFETIME:
        raise AdminError("登录已失效，请关闭后重新打开小程序。", 401)
    if not await is_admin(data.user.id, settings.admin_ids):
        raise AdminError("此页面仅供抽奖管理员使用。", 403)
    return data.user.id


def admin_endpoint(handler):
    @wraps(handler)
    async def authenticated(request: web.Request) -> web.Response:
        try:
            request["admin_id"] = await _admin_id(request)
            response = await handler(request)
        except AdminError as exc:
            response = web.json_response({"error": str(exc)}, status=exc.status)
        except TelegramAPIError:
            response = web.json_response(
                {"error": "Telegram 暂时未能完成操作，请检查结果后重试。"}, status=502
            )
        except web.HTTPRequestEntityTooLarge:
            response = web.json_response({"error": "提交的内容太长，请缩短后再试。"}, status=413)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return authenticated
