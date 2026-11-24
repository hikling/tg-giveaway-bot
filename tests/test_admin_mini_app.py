from __future__ import annotations

import asyncio
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode
from uuid import uuid4

import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from bot.db.models import Giveaway, PrizeCode
from bot.db.repo import add_admin, remove_admin
from bot.settings import Settings
from bot.web.routes import register_web_routes
from tests.conftest import ADMIN_ID, BOT_ID
from tests.test_named_multi_publish import PostingBot

BOT_TOKEN = "123456789:AAHunit-test-admin-token"
DB_ADMIN_ID = 2002
NON_ADMIN_ID = 2003


def admin_headers(
    user_id: int = ADMIN_ID,
    *,
    token: str = BOT_TOKEN,
    age: timedelta = timedelta(),
) -> dict[str, str]:
    values = {
        "auth_date": str(int((datetime.now(UTC) - age).timestamp())),
        "user": json.dumps({"id": user_id, "first_name": "Test"}),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    key = hmac.digest(b"WebAppData", token.encode(), "sha256")
    values["hash"] = hmac.digest(key, check.encode(), "sha256").hex()
    return {"Authorization": "tma " + urlencode(values)}


@pytest_asyncio.fixture
async def mini_app(sessionmaker):
    bot = PostingBot()
    bot.set_member("@news", BOT_ID, "administrator")
    bot.set_member("-100222", BOT_ID, "administrator")
    bot.set_chat_menu_button = AsyncMock()
    scheduler = SimpleNamespace(schedule=Mock(), cancel=Mock())
    app = web.Application()
    app["settings"] = Settings(
        _env_file=None,
        bot_token=BOT_TOKEN,
        admin_ids=[ADMIN_ID],
        mini_app_base_url="https://giveaway.example",
    )
    app["bot"] = bot
    app["scheduler"] = scheduler
    register_web_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client, bot, scheduler


async def test_enabled_mini_app_serves_page_and_assets(mini_app):
    client, _, _ = mini_app
    response = await client.get("/admin/")
    assert response.status == 200 and response.content_type == "text/html"
    page = await response.text()
    expected_assets = {
        "/admin/assets/app.css": {"text/css"},
        "/admin/assets/app.js": {"application/javascript", "text/javascript"},
    }
    for path, content_types in expected_assets.items():
        assert path in page
        response = await client.get(path)
        assert response.status == 200 and response.content_type in content_types
        assert await response.read()


async def test_disabled_mini_app_preserves_health_route():
    app = web.Application()
    app["settings"] = Settings(_env_file=None, bot_token=BOT_TOKEN, mini_app_base_url="")
    register_web_routes(app)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/healthz")
        assert response.status == 200 and await response.text() == "ok"
        assert (await client.get("/admin/")).status == 404
        assert (await client.get("/admin/api/access")).status == 404


async def test_signed_identity_is_required_and_current_admin_membership_is_checked(
    mini_app, sessionmaker
):
    client, _, _ = mini_app
    path = "/admin/api/access"
    denied = [
        ({}, 401),
        (admin_headers(token="123456789:AAHunit-test-forged"), 401),
        (admin_headers(age=timedelta(days=1)), 401),
        (admin_headers(NON_ADMIN_ID), 403),
    ]
    for headers, status in denied:
        response = await client.get(path, headers=headers)
        assert response.status == status
        assert response.headers["Cache-Control"] == "no-store"
        assert "error" in await response.json()

    response = await client.post(
        path,
        headers=admin_headers(NON_ADMIN_ID),
        json={"action": "add-admin", "user_id": str(NON_ADMIN_ID)},
    )
    assert response.status == 403
    async with sessionmaker() as session:
        await add_admin(session, DB_ADMIN_ID, added_by=ADMIN_ID)
        await session.commit()
    for user_id in (ADMIN_ID, DB_ADMIN_ID):
        response = await client.get(path, headers=admin_headers(user_id))
        assert response.status == 200
        data = await response.json()
        assert data["admins"] == [
            {"id": str(DB_ADMIN_ID), "configured": False},
            {"id": str(ADMIN_ID), "configured": True},
        ]
    async with sessionmaker() as session:
        await remove_admin(session, DB_ADMIN_ID)
        await session.commit()
    response = await client.get(path, headers=admin_headers(DB_ADMIN_ID))
    assert response.status == 403


async def test_create_preview_retry_and_republish_use_same_giveaway(mini_app, sessionmaker):
    client, bot, scheduler = mini_app
    bot.fail_for.add(-100222)
    headers = admin_headers()
    draft = {
        "title": "同场抽奖",
        "channels": "官方频道 | https://t.me/news",
        "groups": "交流群 | https://t.me/+invite | -100222",
        "condition": "2h或满100人",
        "prize_mode": "unique",
        "tiers": [{"name": "一档", "count": 2, "codes": "A\n A \n\nB"}],
    }
    response = await client.post("/admin/api/create/preview", headers=headers, json=draft)
    assert response.status == 200, await response.text()
    preview = await response.json()
    assert preview["tiers"] == [{"name": "一档", "count": 2, "inventory": 2}]
    assert set(preview["destinations"]) == {"官方频道", "交流群"}
    assert not bot.sent
    async with sessionmaker() as session:
        assert not (await session.scalars(select(Giveaway))).all()

    payload = {**draft, "condition": preview["fixed_condition"], "request_id": str(uuid4())}
    responses = await asyncio.gather(
        client.post("/admin/api/create", headers=headers, json=payload),
        client.post("/admin/api/create", headers=headers, json=payload),
    )
    assert all(response.status == 200 for response in responses)
    created, replay = [await response.json() for response in responses]
    gid = created["id"]
    assert created == replay == {"id": gid, "sent": 1, "failed": ["交流群"], "canceled": False}
    assert [chat_id for chat_id, _, _ in bot.sent] == [-100111]
    async with sessionmaker() as session:
        assert [gw.id for gw in await session.scalars(select(Giveaway))] == [gid]
        assert sorted(await session.scalars(select(PrizeCode.code))) == ["A", "B"]
        gw = await session.get(Giveaway, gid)
        assert gw.target_participants == 100
        assert preview["fixed_condition"] == gw.end_at.strftime("%Y-%m-%d %H:%M:%SZ") + "或满100人"
    scheduler.schedule.assert_called_once_with(gid, gw.end_at, "either")

    bot.fail_for.clear()
    response = await client.post(
        f"/admin/api/giveaways/{gid}/actions", headers=headers, json={"action": "republish"}
    )
    assert response.status == 200 and (await response.json())["count"] == 2
    assert [chat_id for chat_id, _, _ in bot.sent] == [-100111, -100222]


async def test_existing_giveaway_can_be_restocked_and_canceled(mini_app, new_giveaway):
    client, _, scheduler = mini_app
    gid = await new_giveaway(prize_mode="unique")
    headers = admin_headers()
    path = f"/admin/api/giveaways/{gid}"
    action = {"tier": "Steam card", "codes": "same\n\n same \nanother"}
    response = await client.post(
        path + "/actions", headers=headers, json={**action, "action": "stock-preview"}
    )
    assert response.status == 200 and (await response.json())["count"] == 2
    response = await client.get(path, headers=headers)
    assert (await response.json())["tiers"][0]["total"] == 0
    response = await client.post(
        path + "/actions", headers=headers, json={**action, "action": "add-stock"}
    )
    assert response.status == 200 and (await response.json())["count"] == 2
    response = await client.get(path, headers=headers)
    assert (await response.json())["tiers"][0]["remaining"] == 2

    response = await client.post(path + "/actions", headers=headers, json={"action": "cancel"})
    assert response.status == 200
    scheduler.cancel.assert_called_once_with(gid)
    response = await client.get(path, headers=headers)
    assert (await response.json())["status"] == "canceled"


async def test_add_admin_grants_app_entry_in_their_private_chat(mini_app):
    client, bot, _ = mini_app
    response = await client.post(
        "/admin/api/access",
        headers=admin_headers(),
        json={"action": "add-admin", "user_id": str(DB_ADMIN_ID)},
    )
    assert response.status == 200
    menu_call = bot.set_chat_menu_button.await_args.kwargs
    assert menu_call["chat_id"] == DB_ADMIN_ID
    assert menu_call["menu_button"].web_app.url == "https://giveaway.example/admin/"
    response = await client.get("/admin/api/access", headers=admin_headers(DB_ADMIN_ID))
    assert response.status == 200
