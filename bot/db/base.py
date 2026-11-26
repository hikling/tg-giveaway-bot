from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from bot.settings import PROJECT_ROOT

log = logging.getLogger("bot.db")


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    return _sessionmaker


# Columns added after the first release.  ``create_all()`` can only create
# *new* tables; it never alters existing ones.  This is the smallest
# sufficient migration mechanism for this project — no Alembic, no ceremony,
# just "add the column if the table is missing it".
_LIGHTWEIGHT_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # (table, column, DDL type)
    ("winner_logs", "replaced_at", "DATETIME"),
    ("winner_logs", "notified_ok", "BOOLEAN"),
    ("winner_logs", "notify_error", "VARCHAR(256)"),
    ("participants", "verified_at", "DATETIME"),
    ("participants", "turnstile_nonce", "VARCHAR(64)"),
    ("participants", "turnstile_expires_at", "DATETIME"),
    ("giveaways", "prize_mode", "VARCHAR(16) DEFAULT 'none'"),
    ("giveaways", "draw_seed", "VARCHAR(64)"),
    ("giveaways", "draw_commit", "VARCHAR(64)"),
    ("giveaways", "required_groups", "JSON DEFAULT '[]'"),
    ("giveaways", "draw_mode", "VARCHAR(16) DEFAULT 'time'"),
    ("giveaways", "target_participants", "INTEGER"),
    ("giveaways", "publishing", "BOOLEAN DEFAULT 0"),
    ("poetry_attempts", "expires_at", "DATETIME"),
    ("poetry_attempts", "sent_at", "DATETIME"),
)


def absolutize_sqlite_url(database_url: str) -> str:
    """Resolve a relative SQLite path against the project root.

    ``.env`` is always read from ``PROJECT_ROOT/.env``, but the
    ``database_url`` inside it was previously resolved against the current
    working directory — so starting the bot from a systemd unit, a cron job or
    a double-clicked file created the database in a different, surprising
    place.  Relative paths now always mean "relative to the project root".
    """
    path = sqlite_path_of(database_url)
    if path is None or path.is_absolute():
        return database_url
    url = make_url(database_url).set(database=(PROJECT_ROOT / path).as_posix())
    return url.render_as_string(hide_password=False)


def sqlite_path_of(database_url: str) -> Path | None:
    """Filesystem path behind a SQLite URL, or None for other backends."""
    if not database_url.startswith("sqlite"):
        return None
    # SQLAlchemy 2.1 renders Windows drive letters and non-ASCII path
    # segments with percent escapes. Parse them before testing absoluteness.
    path = make_url(database_url).database
    if not path or path.startswith(":memory:"):
        return None
    return Path(path)


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Apply the SQLite pragmas on **every** connection.

    The previous code ran them inside ``engine.begin()``, i.e. on the one
    connection that happened to be checked out at that moment.  aiosqlite uses
    a ``NullPool``, so each session gets a brand new connection with
    default settings — measured: ``PRAGMA foreign_keys`` read back as 0 on
    the very next connection.  ``journal_mode=WAL`` happens to be persistent
    (it is a database-level property) so it survived, but ``foreign_keys`` is
    per-connection and did not.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    @event.listens_for(engine.sync_engine, "connect")
    def _verify_sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys")
            if cursor.fetchone()[0] != 1:
                log.warning("SQLite foreign_keys pragma did not take effect")
        except Exception:
            log.exception("could not verify SQLite pragmas")
        finally:
            cursor.close()


async def _apply_lightweight_migrations(conn) -> None:
    for table, column, ddl_type in _LIGHTWEIGHT_MIGRATIONS:
        try:
            result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
            existing = {row[1] for row in result.fetchall()}
        except Exception:
            log.exception("migration probe failed for %s", table)
            continue
        if not existing:
            # Table is new; create_all() will create it with the new columns.
            continue
        if column in existing:
            continue
        await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
        if table == "poetry_attempts" and column == "expires_at":
            # 升级前已发出的旧题统一获得60秒作答时间，仅在首次添加列时执行。
            await conn.exec_driver_sql(
                "UPDATE poetry_attempts SET expires_at=datetime('now', '+60 seconds') WHERE nonce IS NOT NULL"
            )
        if table == "poetry_attempts" and column == "sent_at":
            await conn.exec_driver_sql(
                "UPDATE poetry_attempts SET sent_at=datetime('now') WHERE nonce IS NOT NULL"
            )
        log.info("migration: added %s.%s", table, column)
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_poetry_attempts_expires_at ON poetry_attempts (expires_at)"
    )


async def init_db(database_url: str) -> None:
    global _engine, _sessionmaker

    database_url = absolutize_sqlite_url(database_url)
    db_path = sqlite_path_of(database_url)
    if db_path is not None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        log.info("sqlite path: %s", db_path)

    engine_kwargs: dict[str, object] = {
        "future": True,
        "echo": False,
        "hide_parameters": True,
    }
    if database_url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"timeout": 30}
    _engine = create_async_engine(database_url, **engine_kwargs)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)

    if database_url.startswith("sqlite"):
        _install_sqlite_pragmas(_engine)

    from bot.db import models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if database_url.startswith("sqlite"):
            await _apply_lightweight_migrations(conn)
    log.info("db ready: %s", database_url.split("://", 1)[0])
