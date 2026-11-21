"""The three anti-abuse gates that used to live in an unused folder.

They are now on the request path: account-age on join, per-IP budget on the
verification page, commit-reveal on the draw.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from bot.db.models import GiveawayStatus
from bot.db.repo import add_participant, count_participants, get_giveaway
from bot.handlers import participate as participate_mod
from bot.services import giveaway as gw_service
from bot.services.giveaway import _pick_tier, finalize_and_publish
from bot.utils.antiabuse import draw_indices, reveal_ok

ADMIN_ID = 424242


class FakeCQ:
    def __init__(self, user_id: int) -> None:
        self.from_user = SimpleNamespace(id=user_id, username=None)
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def _on_join():
    router = participate_mod.build_participate_router()
    for handler in router.callback_query.handlers:
        if handler.callback.__name__ == "on_join":
            return handler.callback
    raise AssertionError("on_join is not registered")


@pytest.mark.asyncio
async def test_account_age_gate_refuses_a_fresh_account(
    sessionmaker, new_giveaway, bot, monkeypatch
) -> None:
    gid = await new_giveaway()
    monkeypatch.setattr(
        participate_mod,
        "get_settings",
        lambda: SimpleNamespace(account_age_gate=True, min_user_id=10**12),
    )

    cq = FakeCQ(1001)
    cq.bot = bot
    await _on_join()(cq, SimpleNamespace(giveaway_id=gid))

    assert cq.answers[-1][1] is True, "the refusal must be an alert"
    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0


@pytest.mark.asyncio
async def test_account_age_gate_lets_old_accounts_through(
    sessionmaker, new_giveaway, bot, monkeypatch
) -> None:
    gid = await new_giveaway()
    monkeypatch.setattr(
        participate_mod,
        "get_settings",
        lambda: SimpleNamespace(account_age_gate=True, min_user_id=1000),
    )

    cq = FakeCQ(1001)
    cq.bot = bot
    await _on_join()(cq, SimpleNamespace(giveaway_id=gid))

    async with sessionmaker() as session:
        assert await count_participants(session, gid) == 0


@pytest.mark.asyncio
async def test_new_giveaway_commits_to_a_seed_before_it_opens(sessionmaker) -> None:
    gw = await gw_service.create(
        creator_id=ADMIN_ID,
        title="commit-reveal",
        description="",
        prize_tiers=[{"name": "Gold", "count": 1}],
        required_channels=[],
        end_at=datetime.now(UTC) + timedelta(hours=1),
        turnstile_required=False,
    )
    assert gw.draw_seed and len(gw.draw_seed) == 64
    assert gw.draw_commit and reveal_ok(gw.draw_seed, gw.draw_commit)


def test_tier_draw_is_reproducible_from_the_seed() -> None:
    people = [SimpleNamespace(user_id=5000 + i) for i in range(25)]

    first, _ = _pick_tier(people, "Gold", 3, set(), set(), seed="deadbeef")
    second, _ = _pick_tier(people, "Gold", 3, set(), set(), seed="deadbeef")
    assert [p.user_id for p in first] == [p.user_id for p in second]

    expected = [people[i] for i in draw_indices("deadbeef|Gold", 25, 3)]
    assert [p.user_id for p in first] == [p.user_id for p in expected]

    other, _ = _pick_tier(people, "Gold", 3, set(), set(), seed="cafebabe")
    assert [p.user_id for p in other] != [p.user_id for p in first]


@pytest.mark.asyncio
async def test_the_announcement_reveals_the_seed(sessionmaker, bot) -> None:
    gw = await gw_service.create(
        creator_id=ADMIN_ID,
        title="reveal",
        description="",
        prize_tiers=[{"name": "Gold", "count": 1}],
        required_channels=[],
        end_at=datetime.now(UTC) + timedelta(hours=1),
        turnstile_required=False,
    )
    gid = gw.id
    async with sessionmaker() as session:
        await add_participant(session, giveaway_id=gid, user_id=1001, username="u")
        await session.commit()
    await gw_service.attach_message(gid, chat_id=-100777, message_id=1)

    await finalize_and_publish(gid, bot)

    async with sessionmaker() as session:
        gw = await get_giveaway(session, gid)
    assert gw.status == GiveawayStatus.finished
    public = [text for chat, text, _kw in bot.sent if chat == -100777][0]
    assert gw.draw_seed in public
    assert gw.draw_commit in public
    assert "随机种子" in public


class FakeMessage:
    def __init__(self, user_id: int = ADMIN_ID) -> None:
        self.from_user = SimpleNamespace(id=user_id, username="admin")
        self.text = ""
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


@pytest.mark.asyncio
async def test_gate_command_reports_the_current_policy(sessionmaker, new_giveaway) -> None:
    from bot.handlers.admin_manage import build_admin_manage_router

    gid = await new_giveaway()
    async with sessionmaker() as session:
        await add_participant(session, giveaway_id=gid, user_id=1001, username="u")
        await session.commit()

    handler = next(
        h.callback
        for h in build_admin_manage_router().message.handlers
        if h.callback.__name__ == "on_gate"
    )
    message = FakeMessage()
    await handler(message, SimpleNamespace(args=str(gid)))

    text = message.answers[-1]
    assert "古诗词" in text
    assert "1001" in text, text
