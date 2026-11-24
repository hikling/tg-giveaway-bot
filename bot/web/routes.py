from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import web

log = logging.getLogger("bot.web.routes")
_ADMIN_ASSETS = Path(__file__).with_name("admin_assets")


async def admin_page(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        _ADMIN_ASSETS / "index.html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' https://telegram.org; "
                "object-src 'none'; base-uri 'none'; form-action 'self'; "
                "frame-ancestors https://*.telegram.org"
            ),
            "Referrer-Policy": "no-referrer",
        },
    )


async def admin_asset(request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        _ADMIN_ASSETS / request.match_info["name"], headers={"Cache-Control": "no-cache"}
    )


async def healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


def register_web_routes(app: web.Application) -> None:
    app.router.add_get("/healthz", healthz)
    settings = app.get("settings")
    if settings is not None and settings.mini_app_base_url:
        from bot.web import admin_create, admin_manage
        from bot.web.admin_common import admin_endpoint

        app["admin_create_lock"] = asyncio.Lock()
        app.router.add_get("/admin/", admin_page)
        app.router.add_get(r"/admin/assets/{name:app\.(css|js)}", admin_asset)
        app.router.add_get("/admin/api/giveaways", admin_endpoint(admin_manage.giveaway_index))
        app.router.add_get(
            "/admin/api/giveaways/{id}", admin_endpoint(admin_manage.giveaway_detail)
        )
        app.router.add_post(
            "/admin/api/giveaways/{id}/actions", admin_endpoint(admin_manage.giveaway_action)
        )
        app.router.add_post("/admin/api/create/preview", admin_endpoint(admin_create.preview))
        app.router.add_post("/admin/api/create", admin_endpoint(admin_create.create))
        app.router.add_get("/admin/api/access", admin_endpoint(admin_manage.access_details))
        app.router.add_post("/admin/api/access", admin_endpoint(admin_manage.access_action))
    log.info("web routes registered: /healthz")
