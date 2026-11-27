from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from bot.utils.ids import DatabaseId


class JoinCb(CallbackData, prefix="join"):
    giveaway_id: DatabaseId


class AdminGwCb(CallbackData, prefix="agw"):
    action: str
    giveaway_id: DatabaseId


class ClaimCb(CallbackData, prefix="claim"):
    """Claim button on the winner DM.

    The tier travels as an index, not as its name: tier names are free text
    and would blow past Telegram's 64-byte callback_data limit.
    """

    giveaway_id: DatabaseId
    tier_index: int


class PoetryCb(CallbackData, prefix="poem"):
    giveaway_id: DatabaseId
    nonce: str
    choice: int


class PrizeDraftCb(CallbackData, prefix="prizedraft"):
    scope: str
    owner: DatabaseId
    token: str
    action: str
