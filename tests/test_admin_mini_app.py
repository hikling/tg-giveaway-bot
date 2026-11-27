from __future__ import annotations

import asyncio
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode
from uuid import uuid4

import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from bot.db.models import Giveaway, GiveawayStatus, PrizeCode, WinnerLog
from bot.db.repo import (
    add_admin,
    add_participant,
    ban_user,
    ensure_user,
    log_winners,
    mark_winner_notified,
    remove_admin,
)
from bot.prize_policy import CLAIM_WINDOW
from bot.services import prize as prize_service
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
    assert response.headers["Cache-Control"] == "no-store"
    page = await response.text()
    assert 'name="prize_mode" value="shared"' not in page
    expected_assets = {
        "/admin/assets/app.css": {"text/css"},
        "/admin/assets/app.js": {"application/javascript", "text/javascript"},
    }
    for path, content_types in expected_assets.items():
        version = re.search(rf'"{re.escape(path)}\?v=([0-9a-f]{{16}})"', page)
        assert version is not None
        response = await client.get(f"{path}?v={version[1]}")
        assert response.status == 200 and response.content_type in content_types
        content = await response.read()
        assert sha256(content).hexdigest()[:16] == version[1]


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
    assert "一人一码" in preview["post_preview"]
    assert "48 小时" in preview["post_preview"] and "自动回收" in preview["post_preview"]
    assert '<a href="https://t.me/news">官方频道</a>' in preview["post_preview"]
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


async def test_index_search_counts_detail_winners_and_access_management(
    mini_app, sessionmaker, new_giveaway
):
    client, bot, _ = mini_app
    headers = admin_headers()
    gid_running = await new_giveaway(
        title="Spring Steam Gift",
        draw_seed="secret-seed-do-not-leak",
        draw_commit="a" * 64,
        required_channels=["@news|https://t.me/news|官方频道"],
    )
    gid_finished = await new_giveaway(
        title="Autumn Key Drop",
        draw_seed="revealed-seed",
        draw_commit="b" * 64,
        prize_mode="unique",
    )
    async with sessionmaker() as session:
        gw_fin = await session.get(Giveaway, gid_finished)
        gw_fin.status = GiveawayStatus.finished
        await ensure_user(session, 1001, "lucky_winner")
        await add_participant(session, giveaway_id=gid_finished, user_id=1001, username="lucky_winner")
        rows = await log_winners(session, gid_finished, [(1001, "Steam card")])
        await mark_winner_notified(session, rows[0].id, ok=False, error="Forbidden")
        await ban_user(session, 9001, reason="spam bot", banned_by=ADMIN_ID)
        await add_admin(session, DB_ADMIN_ID, added_by=ADMIN_ID)
        await session.commit()

    # Counts and search filtering
    res = await client.get("/admin/api/giveaways?status=all", headers=headers)
    assert res.status == 200
    listing = await res.json()
    assert listing["counts"] == {"running": 1, "finished": 1, "canceled": 0, "all": 2}

    res = await client.get("/admin/api/giveaways?status=all&q=Autumn", headers=headers)
    assert [item["id"] for item in (await res.json())["items"]] == [gid_finished]

    # Running giveaway must not expose draw_seed
    res = await client.get(f"/admin/api/giveaways/{gid_running}", headers=headers)
    running_detail = await res.json()
    assert running_detail["draw_commit"] == "a" * 64
    assert running_detail["draw_seed"] is None
    assert running_detail["requirements"]["channels"] == [
        {"name": "官方频道", "url": "https://t.me/news"}
    ]

    # Finished giveaway exposes draw_seed, winners, and rerollable flag
    res = await client.get(f"/admin/api/giveaways/{gid_finished}", headers=headers)
    fin_detail = await res.json()
    assert fin_detail["draw_seed"] == "revealed-seed"
    assert fin_detail["tiers"][0]["rerollable"] is True
    assert fin_detail["winners"][0]["user_id"] == "1001"
    assert fin_detail["winners"][0]["username"] == "lucky_winner"
    assert fin_detail["winners"][0]["notified_ok"] is False
    assert fin_detail["winners"][0]["claim_expired"] is False

    await prize_service.add_codes(gid_finished, "Steam card", ["PRIVATE-UNISSUED-CODE"])
    async with sessionmaker() as session:
        winner = await session.get(WinnerLog, rows[0].id)
        winner.drawn_at -= CLAIM_WINDOW
        await session.commit()
    detail_path = f"/admin/api/giveaways/{gid_finished}"
    res = await client.get(detail_path, headers=headers)
    expired_detail = await res.json()
    assert expired_detail["winners"][0]["claim_expired"] is True
    assert expired_detail["pending_notifications"] == 0
    assert expired_detail["tiers"][0]["recoverable"] == 1
    assert expired_detail["can_withdraw_codes"] is True
    assert "PRIVATE-UNISSUED-CODE" not in await res.text()
    res = await client.get(detail_path, headers=admin_headers(DB_ADMIN_ID))
    assert (await res.json())["can_withdraw_codes"] is False

    # Access lists blacklist and lets admin remove dynamic DB admins (but not configured or self)
    res = await client.get("/admin/api/access", headers=headers)
    access = await res.json()
    assert access["current_admin_id"] == str(ADMIN_ID)
    assert access["blacklist"][0]["user_id"] == "9001"
    assert access["blacklist"][0]["reason"] == "spam bot"

    res = await client.post(
        "/admin/api/access",
        headers=headers,
        json={"action": "remove-admin", "user_id": str(ADMIN_ID)},
    )
    assert res.status == 400

    res = await client.post(
        "/admin/api/access",
        headers=headers,
        json={"action": "remove-admin", "user_id": str(DB_ADMIN_ID)},
    )
    assert res.status == 200
    assert (await client.get("/admin/api/access", headers=admin_headers(DB_ADMIN_ID))).status == 403
    assert bot.set_chat_menu_button.await_count >= 1


async def test_removing_a_recent_location_persists_for_the_current_admin(
    mini_app, sessionmaker, new_giveaway
):
    client, _, _ = mini_app
    async with sessionmaker() as session:
        await add_admin(session, DB_ADMIN_ID, added_by=ADMIN_ID)
        await session.commit()
    history_id = await new_giveaway(
        required_channels=["@NEWS|https://t.me/news|旧频道名"],
        required_groups=["-100222|https://t.me/+invite|交流群"],
    )
    headers = admin_headers()
    path = "/admin/api/locations"
    payload = {"kind": "channels", "reference": "@news"}
    assert (
        await client.post(path + "/remove", headers=admin_headers(NON_ADMIN_ID), json=payload)
    ).status == 403
    for _ in range(2):
        assert (await client.post(path + "/remove", headers=headers, json=payload)).status == 200

    await new_giveaway(required_channels=["@news|https://t.me/news|新频道名"])
    mine = await (await client.get(path, headers=headers)).json()
    assert mine["channels"] == []
    assert mine["groups"][0]["reference"] == "-100222"
    other = await (await client.get(path, headers=admin_headers(DB_ADMIN_ID))).json()
    assert [item["name"] for item in other["channels"]] == ["新频道名"]
    async with sessionmaker() as session:
        giveaway = await session.get(Giveaway, history_id)
        assert giveaway.required_channels == ["@NEWS|https://t.me/news|旧频道名"]


async def test_new_giveaway_rejects_shared_text_and_escapes_preview(mini_app):
    client, _, _ = mini_app
    headers = admin_headers()
    draft = {
        "title": "<img src=x onerror=alert(1)>",
        "channels": "官方频道 | https://t.me/news",
        "condition": "1h",
        "prize_mode": "shared",
        "tiers": [{"name": "一档", "count": 2}],
    }
    for path, payload in [
        ("/create/preview", draft),
        ("/create", {**draft, "request_id": str(uuid4())}),
    ]:
        response = await client.post("/admin/api" + path, headers=headers, json=payload)
        assert response.status == 400

    response = await client.post(
        "/admin/api/create/preview", headers=headers, json={**draft, "prize_mode": "none"}
    )
    assert response.status == 200
    preview = (await response.json())["post_preview"]
    assert "&lt;img src=x onerror=alert(1)&gt;" in preview
    assert "<img" not in preview
    assert "主办方联系发奖" in preview


async def test_locations_endpoint_single_or_multi_and_canceled_stock_guard(
    mini_app, new_giveaway, monkeypatch
):
    client, bot, _ = mini_app
    headers = admin_headers()
    await new_giveaway(
        required_channels=["@news|https://t.me/news|官方频道"],
        required_groups=["-100222|https://t.me/+invite|交流群"],
    )

    res = await client.get("/admin/api/locations", headers=headers)
    assert res.status == 200
    locs = await res.json()
    assert locs["channels"] == [
        {
            "name": "官方频道", "line": "官方频道 | https://t.me/news",
            "private": False, "reference": "@news",
        }
    ]
    assert locs["groups"] == [
        {
            "name": "交流群", "line": "交流群 | https://t.me/+invite | -100222",
            "private": True, "reference": "-100222",
        }
    ]

    # Single channel only
    res = await client.post(
        "/admin/api/create/preview",
        headers=headers,
        json={
            "title": "单频道抽奖",
            "channels": "单频道 | https://t.me/news",
            "groups": "",
            "condition": "1h",
            "prize_mode": "none",
            "tiers": [{"name": "一等奖", "count": 1}],
        },
    )
    assert res.status == 200, await res.text()
    assert (await res.json())["destinations"] == ["单频道"]

    # Single group only
    res = await client.post(
        "/admin/api/create/preview",
        headers=headers,
        json={
            "title": "单群组抽奖",
            "channels": "",
            "groups": "单群 | https://t.me/+invite | -100222",
            "condition": "1h",
            "prize_mode": "none",
            "tiers": [{"name": "一等奖", "count": 1}],
        },
    )
    assert res.status == 200, await res.text()
    assert (await res.json())["destinations"] == ["单群"]

    # Multiple channels and multiple groups
    orig_get_chat = bot.get_chat

    async def multi_get_chat(ref):
        if str(ref) == "@ch2":
            return SimpleNamespace(id=-100444, type="channel", title="频道B")
        return await orig_get_chat(ref)

    monkeypatch.setattr(bot, "get_chat", multi_get_chat)
    bot.set_member("@ch2", BOT_ID, "administrator")
    bot.set_member("-100333", BOT_ID, "administrator")
    res = await client.post(
        "/admin/api/create/preview",
        headers=headers,
        json={
            "title": "多频道多群组抽奖",
            "channels": "频道A | https://t.me/news\n频道B | https://t.me/ch2",
            "groups": "群A | https://t.me/+inv1 | -100222\n群B | https://t.me/+inv2 | -100333",
            "condition": "1h",
            "prize_mode": "none",
            "tiers": [{"name": "一等奖", "count": 1}],
        },
    )
    assert res.status == 200, await res.text()
    assert set((await res.json())["destinations"]) == {"频道A", "频道B", "群A", "群B"}

    # Canceled giveaway cannot be restocked
    gid = await new_giveaway(prize_mode="unique")
    assert (
        await client.post(
            f"/admin/api/giveaways/{gid}/actions", headers=headers, json={"action": "cancel"}
        )
    ).status == 200
    res = await client.post(
        f"/admin/api/giveaways/{gid}/actions",
        headers=headers,
        json={"action": "add-stock", "tier": "Steam card", "codes": "X"},
    )
    assert res.status == 409
