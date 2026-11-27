"""识别数量、确认前清空、多次粘贴、权限与重复按钮防护。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select

from bot.callbacks.factories import PrizeDraftCb
from bot.db.models import Giveaway, PrizeCode
from bot.db.repo import add_admin
from bot.handlers.admin_create import GiveawayDraft, build_admin_create_router
from bot.handlers.prize import AddCodesDraft, build_prize_admin_router
from bot.services import prize as prizes
from bot.utils.prize_input import normalize_codes, parse_input
from tests.test_named_multi_publish import PostingBot

ADMIN = 424242


class Message:
    def __init__(self, text="", uid=ADMIN):
        self.text = text
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(id=uid, type="private")
        self.responses = []

    async def answer(self, text, **kwargs):
        self.responses.append((text, kwargs))
        return SimpleNamespace(chat=self.chat, message_id=len(self.responses))


def handler(name, *, stock=False, callback=False):
    router = build_prize_admin_router() if stock else build_admin_create_router()
    return next(
        h.callback
        for h in getattr(router, "callback_query" if callback else "message").handlers
        if h.callback.__name__ == name
    )


def context(uid=ADMIN):
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=777000, chat_id=uid, user_id=uid)
    )


async def test_group_commands_do_not_open_a_prize_draft(sessionmaker):
    message = Message("/newgiveaway")
    message.chat = SimpleNamespace(id=-1004351, type="supergroup")
    state = context()
    scheduler = Mock()

    await handler("on_new")(message, state, scheduler)
    await handler("on_addcodes", stock=True)(
        message, CommandObject(command="addcodes", args="1 一等奖"), state, scheduler
    )

    assert await state.get_state() is None
    assert len(message.responses) == 2
    assert all("私聊" in text for text, _ in message.responses)
    assert [call.args for call in scheduler.schedule_group_message_deletion.call_args_list] == [
        (message.chat.id, 1),
        (message.chat.id, 2),
    ]


async def setup_creation(*, tiers=None):
    state = context()
    await state.set_state(GiveawayDraft.waiting_prizemode)
    await state.update_data(
        title="抽奖",
        description="",
        prizes=tiers or [{"name": "一等奖", "count": 2}],
        channels=["@news"],
        groups=[],
        end_at_iso=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    await handler("step_prizemode")(Message("1"), state)
    return state


async def input_codes(state, text, *, stock=False):
    message = Message(text)
    await handler("step_codes", stock=stock)(message, state)
    return message


def button(message, action):
    keyboard = message.responses[-1][1]["reply_markup"]
    return next(
        PrizeDraftCb.unpack(b.callback_data)
        for row in keyboard.inline_keyboard
        for b in row
        if PrizeDraftCb.unpack(b.callback_data).action == action
    )


async def click(state, data, *, stock=False, uid=ADMIN):
    # 按钮消息由机器人发送，不能把机器人当作草稿所有者。
    cq = SimpleNamespace(
        from_user=SimpleNamespace(id=uid), message=Message(uid=777000), answer=AsyncMock()
    )
    await handler("on_stock_action" if stock else "on_codes_action", stock=stock, callback=True)(
        cq, data, state
    )
    return cq


@pytest.fixture
async def admin(sessionmaker):
    async with sessionmaker() as session:
        await add_admin(session, ADMIN)
        await session.commit()


async def rows(sessionmaker, gid=None):
    async with sessionmaker() as session:
        stmt = select(PrizeCode)
        if gid is not None:
            stmt = stmt.where(PrizeCode.giveaway_id == gid)
        return (await session.execute(stmt)).scalars().all()


def test_normalizer_matches_empty_and_duplicate_rules():
    assert normalize_codes([" A ", "A", "", "  ", "B"]) == ["A", "B"]
    assert parse_input(" A\n\nB\nA ", "unique") == ["A", "B"]
    assert parse_input("B\n C", "unique", ["A", "B"]) == ["A", "B", "C"]
    assert parse_input("一行\n二行", "shared") == ["一行\n二行"]


async def test_recognized_count_is_shown_without_saving_or_echoing_codes(sessionmaker):
    state = await setup_creation()
    message = await input_codes(state, "SECRET-1\n\nSECRET-1\n SECRET-2 ")
    data = await state.get_data()
    assert data["codes_pending"] == ["SECRET-1", "SECRET-2"] and data["codes_buffer"] == []
    assert await state.get_state() == GiveawayDraft.waiting_codes_review.state
    text = message.responses[-1][0]
    assert "已识别：2 条" in text and "计划中奖：2 名" in text
    assert "SECRET-" not in text
    assert not await rows(sessionmaker)


async def test_multiple_pastes_accumulate_and_invalidate_old_buttons(sessionmaker, admin):
    state = await setup_creation()
    first = await input_codes(state, "A\nB")
    old = button(first, "accept")
    second = await input_codes(state, "B\nC")
    assert "已识别：3 条" in second.responses[-1][0]
    await click(state, old)
    assert await state.get_state() == GiveawayDraft.waiting_codes_review.state
    await click(state, button(second, "accept"))
    assert (await state.get_data())["codes_buffer"] == [["一等奖", ["A", "B", "C"]]]


async def test_reset_clears_all_tiers_and_preserves_other_settings(sessionmaker, admin):
    state = await setup_creation(
        tiers=[{"name": "一等奖", "count": 1}, {"name": "二等奖", "count": 2}]
    )
    first = await input_codes(state, "OLD-1")
    await click(state, button(first, "accept"))
    second = await input_codes(state, "OLD-2\nOLD-3")
    await click(state, button(second, "reset"))
    data = await state.get_data()
    assert data["codes_index"] == 0 and data["codes_pending"] == [] and data["codes_buffer"] == []
    assert data["title"] == "抽奖" and data["channels"] == ["@news"]
    assert [p["count"] for p in data["prizes"]] == [1, 2]
    assert await state.get_state() == GiveawayDraft.waiting_codes.state
    fresh = await input_codes(state, "NEW")
    await click(state, button(fresh, "accept"))
    assert (await state.get_data())["codes_buffer"] == [["一等奖", ["NEW"]]]


async def test_reset_is_available_on_final_confirmation_and_keeps_owner(sessionmaker, admin):
    state = await setup_creation()
    first = await input_codes(state, "OLD-1\nOLD-2")
    cq = await click(state, button(first, "accept"))
    final = cq.message
    assert "已录入 2 条" in final.responses[-1][0]
    reset = button(final, "reset")
    assert reset.owner == ADMIN
    await click(state, reset)
    data = await state.get_data()
    assert not data["codes_buffer"] and data["codes_index"] == 0


async def test_reset_text_on_final_confirmation_does_not_publish(sessionmaker):
    state = await setup_creation()
    await input_codes(state, "OLD")
    await input_codes(state, "确认奖品")
    message = Message("全部删除并重新添加")
    bot = SimpleNamespace(get_chat=AsyncMock())
    await handler("step_confirm")(message, state, bot, SimpleNamespace(schedule=Mock()))
    bot.get_chat.assert_not_awaited()
    assert (await state.get_data())["codes_buffer"] == []
    assert await state.get_state() == GiveawayDraft.waiting_codes.state


async def test_duplicate_accept_does_not_skip_next_tier(sessionmaker, admin):
    state = await setup_creation(tiers=[{"name": "一档", "count": 1}, {"name": "二档", "count": 1}])
    message = await input_codes(state, "A")
    accept = button(message, "accept")
    await asyncio.gather(click(state, accept), click(state, accept))
    data = await state.get_data()
    assert data["codes_index"] == 1 and data["codes_buffer"] == [["一档", ["A"]]]
    assert await state.get_state() == GiveawayDraft.waiting_codes.state


async def test_old_button_cannot_reset_new_draft(sessionmaker, admin):
    state = await setup_creation()
    old = button(await input_codes(state, "OLD"), "reset")
    await handler("on_new")(Message("/newgiveaway"), state, Mock())
    before = await state.get_data()
    await click(state, old)
    assert await state.get_data() == before
    assert await state.get_state() == GiveawayDraft.waiting_title.state


async def test_other_admin_and_nonadmin_cannot_use_owner_buttons(sessionmaker, admin):
    state = await setup_creation()
    data = button(await input_codes(state, "KEEP"), "reset")
    async with sessionmaker() as session:
        await add_admin(session, 999)
        await session.commit()
    before = await state.get_data()
    await click(state, data, uid=999)
    await click(state, data, uid=123)
    assert await state.get_data() == before


async def test_confirm_summary_escapes_tier_name_once(sessionmaker):
    state = await setup_creation(tiers=[{"name": "R&D <奖品>", "count": 1}])
    await input_codes(state, "A")
    message = await input_codes(state, "确认奖品")
    text = message.responses[-1][0]
    assert "R&amp;D &lt;奖品&gt;" in text
    assert "&amp;amp;" not in text and "<奖品>" not in text


async def test_replacement_codes_only_are_saved_and_publish_once(sessionmaker):
    state = await setup_creation()
    await input_codes(state, "OLD")
    await input_codes(state, "全部删除并重新添加")
    await input_codes(state, "NEW-1\nNEW-1\nNEW-2")
    await input_codes(state, "确认奖品")
    bot = PostingBot()
    bot.set_member("@news", 777000, "administrator")
    schedule = SimpleNamespace(schedule=Mock())
    await asyncio.gather(
        handler("step_confirm")(Message("确认"), state, bot, schedule),
        handler("step_confirm")(Message("确认"), state, bot, schedule),
    )
    stock = await rows(sessionmaker)
    assert [p.code for p in stock] == ["NEW-1", "NEW-2"]
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Giveaway)) == 1
    assert len([text for _, text, _ in bot.sent if "🏆" in text]) == 1


async def test_inventory_save_failure_rolls_back_entire_creation_and_keeps_draft(
    sessionmaker, monkeypatch
):
    from bot.services import giveaway

    state = await setup_creation()
    await input_codes(state, "A\nB")
    await input_codes(state, "确认奖品")
    original = giveaway.store_codes

    async def fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("模拟存储故障")

    monkeypatch.setattr(giveaway, "store_codes", fail)
    bot = PostingBot()
    bot.set_member("@news", 777000, "administrator")
    message = Message("确认")
    await handler("step_confirm")(message, state, bot, SimpleNamespace(schedule=Mock()))
    assert "草稿已保留" in message.responses[-1][0]
    assert await state.get_state() == GiveawayDraft.waiting_confirm.state
    assert not await rows(sessionmaker)
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Giveaway)) == 0
    assert not bot.sent


async def stock_draft(gid):
    state = context()
    message = Message()
    await handler("on_addcodes", stock=True)(
        message, CommandObject(command="addcodes", args=f"{gid} Steam card"), state, Mock()
    )
    return state


async def test_replenishment_count_requires_confirmation_and_reset_does_not_touch_stock(
    sessionmaker, new_giveaway, admin
):
    gid = await new_giveaway(prize_mode="unique")
    await prizes.add_codes(gid, "Steam card", ["EXISTING"])
    state = await stock_draft(gid)
    first = await input_codes(state, "BAD-1\nBAD-1\nBAD-2", stock=True)
    assert "已识别：2" in first.responses[-1][0]
    assert [row.code for row in await rows(sessionmaker, gid)] == ["EXISTING"]
    await click(state, button(first, "reset"), stock=True)
    fresh = await input_codes(state, "GOOD", stock=True)
    await click(state, button(fresh, "accept"), stock=True)
    assert [row.code for row in await rows(sessionmaker, gid)] == ["EXISTING", "GOOD"]
    assert await state.get_state() is None


async def test_replenishment_duplicate_confirm_cannot_add_twice(sessionmaker, new_giveaway, admin):
    gid = await new_giveaway(prize_mode="unique")
    state = await stock_draft(gid)
    review = await input_codes(state, "A\nA\nB", stock=True)
    accept = button(review, "accept")
    await asyncio.gather(click(state, accept, stock=True), click(state, accept, stock=True))
    assert [p.code for p in await rows(sessionmaker, gid)] == ["A", "B"]


async def test_shared_replenishment_preserves_multiline_as_one_row(sessionmaker, new_giveaway):
    gid = await new_giveaway(prize_mode="shared")
    state = await stock_draft(gid)
    review = await input_codes(state, "共享第一行\n共享第二行", stock=True)
    assert "1 份共享文本" in review.responses[-1][0]
    await input_codes(state, "确认入库", stock=True)
    records = await rows(sessionmaker, gid)
    assert len(records) == 1 and records[0].code == "共享第一行\n共享第二行"
    assert records[0].max_claims > 1


async def test_stock_old_buttons_and_cancel_are_safe(sessionmaker, new_giveaway, admin):
    gid = await new_giveaway(prize_mode="unique")
    state = await stock_draft(gid)
    first = await input_codes(state, "A", stock=True)
    old = button(first, "accept")
    await input_codes(state, "B", stock=True)
    await click(state, old, stock=True)
    assert not await rows(sessionmaker, gid)
    await handler("on_cancel_codes", stock=True)(Message("/cancel"), state, Mock())
    await click(state, old, stock=True)
    assert not await rows(sessionmaker, gid)


async def test_replenishment_failure_preserves_pending_input(
    sessionmaker, new_giveaway, monkeypatch
):
    gid = await new_giveaway(prize_mode="unique")
    state = await stock_draft(gid)
    await input_codes(state, "A", stock=True)
    monkeypatch.setattr(prizes, "add_codes", AsyncMock(side_effect=RuntimeError("disk")))
    with pytest.raises(RuntimeError):
        await input_codes(state, "确认入库", stock=True)
    assert (await state.get_data())["stock_pending"] == ["A"]
    assert await state.get_state() == AddCodesDraft.waiting_review.state


def test_callback_size_and_handler_dependency_signatures():
    packed = PrizeDraftCb(scope="stock", owner=2**63 - 1, token="a" * 16, action="accept").pack()
    assert len(packed.encode()) <= 64
    for router in [build_admin_create_router(), build_prize_admin_router()]:
        for entry in router.callback_query.handlers:
            if entry.callback.__name__ in {"on_codes_action", "on_stock_action"}:
                assert {"cq", "state", "callback_data"} <= entry.params
