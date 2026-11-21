from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class JoinCb(CallbackData, prefix="join"):
    giveaway_id: int


class AdminGwCb(CallbackData, prefix="agw"):
    action: str
    giveaway_id: int


class ClaimCb(CallbackData, prefix="claim"):
    """Claim button on the winner DM.

    The tier travels as an index, not as its name: tier names are free text
    and would blow past Telegram's 64-byte callback_data limit.
    """

    giveaway_id: int
    tier_index: int


class PoetryCb(CallbackData, prefix="poem"):
    giveaway_id: int
    nonce: str
    choice: int


class PrizeDraftCb(CallbackData, prefix="prizedraft"):
    scope: str
    owner: int
    token: str
    action: str
