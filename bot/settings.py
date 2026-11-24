from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    bot_token: str = Field(..., min_length=10)
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    webhook_base_url: str = ""
    mini_app_base_url: str = ""
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    # Optional on purpose.  Long polling is the documented default path, but
    # this field used to be "Field(..., min_length=8)", so a perfectly valid
    # long-polling deployment died at import time with a pydantic
    # ValidationError demanding a webhook secret it had no use for.  It is now
    # enforced only in webhook mode (see the model validator below).
    webhook_secret: str = ""

    default_lang: Literal["zh"] = "zh"
    tz: str = "Asia/Shanghai"

    # 可选用户编号下限；不能可靠代表注册年龄。默认0不过滤。
    account_age_gate: bool = True
    min_user_id: int = 0
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"

    log_level: str = "INFO"
    # drop_pending_updates=True on every start silently discarded queued
    # updates after each deploy - including the giveaway post an admin had
    # literally just created.  Off by default; opt in when you are certain.
    drop_pending_updates: bool = False

    @field_validator("default_lang", mode="before")
    @classmethod
    def _chinese_only(cls, v):
        return "zh"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, v):
        if isinstance(v, str):
            if v.strip().startswith("["):
                return json.loads(v)
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @field_validator("webhook_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("mini_app_base_url")
    @classmethod
    def _validate_mini_app_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        error = "MINI_APP_BASE_URL 必须是 HTTPS 站点地址，例如 https://giveaway.example.com"
        try:
            url = urlsplit(value)
            port = url.port
        except ValueError as exc:
            raise ValueError(error) from exc
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or port == 0
            or url.path
            or url.query
            or url.fragment
            or any(char.isspace() for char in value)
            or "?" in value
            or "#" in value
            or "\\" in value
        ):
            raise ValueError(error)
        return value

    @model_validator(mode="after")
    def _webhook_requires_secret(self) -> Settings:
        if self.webhook_base_url and len(self.webhook_secret) < 8:
            raise ValueError(
                "WEBHOOK_SECRET must be set to at least 8 characters when "
                "WEBHOOK_BASE_URL is configured"
            )
        return self

    @property
    def webhook_mode(self) -> bool:
        return bool(self.webhook_base_url)

    @property
    def webhook_path(self) -> str:
        return f"/tg/{quote(self.webhook_secret or 'unset', safe='')}"

    @property
    def webhook_url(self) -> str:
        if not self.webhook_base_url:
            return ""
        return f"{self.webhook_base_url}{self.webhook_path}"

    def masked_webhook_url(self) -> str:
        """Webhook URL safe for logs - the secret is a bearer credential."""
        if not self.webhook_base_url:
            return "(unset)"
        if not self.webhook_secret:
            return f"{self.webhook_base_url}/tg/<no-secret>"
        head = self.webhook_secret[:2]
        tail = self.webhook_secret[-2:]
        return f"{self.webhook_base_url}/tg/{head}***{tail}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
