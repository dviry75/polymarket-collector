from decimal import Decimal

from live.trader_commands import select_verified_redemption


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
