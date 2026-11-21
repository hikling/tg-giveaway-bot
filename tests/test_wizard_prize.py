"""The /newgiveaway wizard gained two steps; these tests pin them down.

Only the FSM transitions are exercised: the final "ok" would publish a post,
which the service-level tests already cover.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers.admin_create import GiveawayDraft, build_admin_create_router
from tests.conftest import ADMIN_ID


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.from_user = type("U", (), {"id": ADMIN_ID, "username": "admin"})()
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


def _handler(name: str):
    router = build_admin_create_router()
    for handler in router.message.handlers:
        if handler.callback.__name__ == name:
            return handler.callback
    raise AssertionError(f"no handler named {name}")


def _ctx() -> FSMContext:
    """A fresh context per test - MemoryStorage keys are (chat, user, bot)."""
    key = StorageKey(bot_id=777000, chat_id=ADMIN_ID, user_id=ADMIN_ID)
    return FSMContext(storage=MemoryStorage(), key=key)


@pytest.mark.asyncio
async def test_unique_codes_are_collected_then_confirmation_is_shown(sessionmaker) -> None:
    fsm = _ctx()
    await fsm.update_data(
        end_at_iso=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        channels=["@news"],
        groups=[],
    )
    await fsm.set_state(GiveawayDraft.waiting_prizemode)
    await fsm.update_data(prizes=[{"name": "Steam card", "count": 2}])

    step_prizemode = _handler("step_prizemode")
    message = FakeMessage("1")
    await step_prizemode(message, fsm)

    data = await fsm.get_data()
    assert data["prize_mode"] == "unique"
    assert await fsm.get_state() == GiveawayDraft.waiting_codes.state
    assert "Steam card" in message.answers[-1]

    step_codes = _handler("step_codes")
    message = FakeMessage("AAA-1\nBBB-2")
    await step_codes(message, fsm)

    assert await fsm.get_state() == GiveawayDraft.waiting_codes_review.state
    assert (await fsm.get_data())["codes_pending"] == ["AAA-1", "BBB-2"]
    await step_codes(FakeMessage("确认奖品"), fsm)
    data = await fsm.get_data()
    assert data["codes_buffer"] == [["Steam card", ["AAA-1", "BBB-2"]]]
    assert await fsm.get_state() == GiveawayDraft.waiting_confirm.state


@pytest.mark.asyncio
async def test_shared_text_keeps_its_newlines(sessionmaker) -> None:
    fsm = _ctx()
    await fsm.update_data(
        end_at_iso=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        channels=["@news"],
        groups=[],
    )
    await fsm.set_state(GiveawayDraft.waiting_prizemode)
    await fsm.update_data(prizes=[{"name": "Steam card", "count": 1}])

    await _handler("step_prizemode")(FakeMessage("2"), fsm)
    assert (await fsm.get_data())["prize_mode"] == "shared"

    text = "line one\nline two"
    await _handler("step_codes")(FakeMessage(text), fsm)
    await _handler("step_codes")(FakeMessage("确认奖品"), fsm)
    buffer = (await fsm.get_data())["codes_buffer"]
    assert buffer == [["Steam card", [text]]]


@pytest.mark.asyncio
async def test_manual_mode_skips_the_codes_step(sessionmaker) -> None:
    fsm = _ctx()
    await fsm.update_data(
        end_at_iso=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        channels=["@news"],
        groups=[],
    )
    await fsm.set_state(GiveawayDraft.waiting_prizemode)
    await fsm.update_data(prizes=[{"name": "Steam card", "count": 1}])

    message = FakeMessage("0")
    await _handler("step_prizemode")(message, fsm)

    assert (await fsm.get_data())["prize_mode"] == "none"
    assert await fsm.get_state() == GiveawayDraft.waiting_confirm.state


@pytest.mark.asyncio
async def test_a_short_batch_warns_but_still_moves_on(sessionmaker) -> None:
    fsm = _ctx()
    await fsm.update_data(
        end_at_iso=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        channels=["@news"],
        groups=[],
    )
    await fsm.set_state(GiveawayDraft.waiting_codes)
    await fsm.update_data(
        prizes=[{"name": "Steam card", "count": 3}],
        prize_mode="unique",
        codes_index=0,
        codes_buffer=[],
    )

    message = FakeMessage("ONLY-ONE")
    await _handler("step_codes")(message, fsm)

    assert any("ONLY" not in text and "1" in text for text in message.answers)
    assert "少 2 条" in message.answers[-1]
    await _handler("step_codes")(FakeMessage("确认奖品"), fsm)
    assert (await fsm.get_data())["codes_buffer"] == [["Steam card", ["ONLY-ONE"]]]
