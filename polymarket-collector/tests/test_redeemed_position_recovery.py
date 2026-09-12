from decimal import Decimal

from live.trader_commands import (
    select_verified_redemption,
    select_verified_unknown_entry_trade,
)


WALLET = "0x" + "1" * 40
CONDITION = "0x" + "2" * 64
TOKEN = "123"
TX_HASH = "0x" + "3" * 64


def activity(**overrides):
    item = {
        "proxyWallet": WALLET,
        "conditionId": CONDITION,
        "asset": TOKEN,
        "type": "REDEEM",
        "size": 5.066665,
        "usdcSize": 5.066665,
        "transactionHash": TX_HASH,
    }
    item.update(overrides)
    return item


def test_select_verified_redemption_requires_exact_identity_and_bounded_size():
    selected = select_verified_redemption(
        [activity()],
        wallet=WALLET,
        condition_id=CONDITION,
        token_id=TOKEN,
        remaining_shares=Decimal("5.0666"),
    )
    assert selected is not None
    assert selected["transactionHash"] == TX_HASH

    invalid = [
        activity(proxyWallet="0x" + "4" * 40),
        activity(conditionId="0x" + "5" * 64),
        activity(asset="wrong"),
        activity(type="TRADE"),
        activity(size=5.0),
        activity(size=5.1),
        activity(transactionHash="bad"),
    ]
    for candidate in invalid:
        assert select_verified_redemption(
            [candidate],
            wallet=WALLET,
            condition_id=CONDITION,
            token_id=TOKEN,
            remaining_shares=Decimal("5.0666"),
        ) is None


def test_select_verified_unknown_entry_trade_requires_one_exact_remote_buy():
    intent = {
        "submitted_at": "2026-09-11T23:03:03+00:00",
        "token_id": "winner-token",
        "condition_id": "0xcondition",
        "price_limit_text": "0.76",
    }
    position = {"acquired_shares_text": "8.2162"}
    trade = {
        "polymarket_trade_id": "trade-1",
        "condition_id": "0xcondition",
        "token_id": "winner-token",
        "side": "BUY",
        "status": "CONFIRMED",
        "price": "0.74",
        "size": "8.216215",
        "transaction_hash": "0x" + "1" * 64,
        "matched_at": "2026-09-11T23:03:04+00:00",
    }
    selected = select_verified_unknown_entry_trade(
        [trade], intent=intent, position=position
    )
    assert selected is not None
    assert selected["verified_shares"] == "8.216215"

    invalid = [
        {**trade, "side": "SELL"},
        {**trade, "size": "5"},
        {**trade, "price": "0.77"},
        {**trade, "transaction_hash": ""},
        {**trade, "matched_at": "2026-09-11T23:10:04+00:00"},
    ]
    for candidate in invalid:
        assert select_verified_unknown_entry_trade(
            [candidate], intent=intent, position=position
        ) is None
    assert select_verified_unknown_entry_trade(
        [trade, {**trade, "polymarket_trade_id": "trade-2"}],
        intent=intent,
        position=position,
    ) is None
