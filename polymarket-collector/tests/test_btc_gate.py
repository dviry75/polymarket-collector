"""Pure BTC reference gate and its configuration."""

from dataclasses import replace
from decimal import Decimal

from live.config import LiveConfig
from live.strategy import BtcGateConfig, btc_reference_gate

CFG = BtcGateConfig()
OPEN = Decimal("80000")


def gate(side, current, *, open_price=OPEN, age=1000, cfg=CFG):
    return btc_reference_gate(side, open_price, current, age, cfg)


def test_up_and_down_pass_on_directional_move():
    up = gate("UP", OPEN + 25)
    assert up.allowed and up.reason == "BTC_GATE_PASS"
    assert up.delta_usd == Decimal("25") and up.directional_delta_usd == Decimal("25")
    down = gate("DOWN", OPEN - 25)
    assert down.allowed and down.directional_delta_usd == Decimal("25")
    assert down.delta_usd == Decimal("-25")


def test_wrong_direction_and_threshold():
    assert gate("UP", OPEN - 25).reason == "BTC_DELTA_BELOW_MIN"
    assert gate("DOWN", OPEN + 25).reason == "BTC_DELTA_BELOW_MIN"
    assert gate("UP", OPEN + Decimal("9.999")).reason == "BTC_DELTA_BELOW_MIN"
    # Exactly the minimum passes.
    assert gate("UP", OPEN + 10).allowed
    assert gate("DOWN", OPEN - 10).allowed


def test_tie_resolves_up():
    zero = BtcGateConfig(min_delta_usd=Decimal("0"))
    assert gate("UP", OPEN, cfg=zero).allowed
    assert gate("DOWN", OPEN, cfg=zero).reason == "BTC_DELTA_BELOW_MIN"
    assert not gate("UP", OPEN).allowed  # default minimum 10


def test_max_delta_only_when_configured():
    assert gate("UP", OPEN + 500).allowed
    capped = BtcGateConfig(max_delta_usd=Decimal("50"))
    assert gate("UP", OPEN + 50, cfg=capped).allowed
    assert gate("UP", OPEN + 51, cfg=capped).reason == "BTC_DELTA_ABOVE_MAX"


def test_fail_closed_reasons():
    assert gate("UP", None).reason == "BTC_FEED_UNAVAILABLE"
    assert gate("UP", OPEN + 20, age=5001).reason == "BTC_PRICE_STALE"
    assert gate("UP", OPEN + 20, age=5000).allowed
    assert gate("UP", OPEN + 20, age=None).reason == "BTC_PRICE_STALE"
    assert gate("UP", OPEN + 20, open_price=None).reason == "BTC_OPEN_PRICE_UNKNOWN"
    unknown = gate(None, OPEN + 20)
    assert unknown.reason == "BTC_SIDE_MAPPING_UNKNOWN" and not unknown.allowed
    assert gate("YES", OPEN + 20).reason == "BTC_SIDE_MAPPING_UNKNOWN"


def test_config_defaults_and_ranges():
    config = LiveConfig()
    assert config.btc_gate_mode == "observe"
    assert config.btc_gate_min_delta_usd == Decimal("10")
    assert config.btc_gate_max_delta_usd is None
    assert config.btc_max_price_age_ms == 5000
    assert not [e for e in config.validation_errors() if "BTC" in e]

    def errors(**changes):
        return [e for e in replace(config, **changes).validation_errors() if "BTC" in e]

    assert errors(btc_gate_mode="enforce") == []
    assert errors(btc_gate_min_delta_usd=Decimal("25")) == []
    assert errors(btc_gate_mode="block")
    assert errors(btc_gate_min_delta_usd=Decimal("-1"))
    assert errors(btc_gate_max_delta_usd=Decimal("10"))
    assert errors(btc_gate_max_delta_usd=Decimal("NaN"))
    assert errors(btc_gate_max_delta_usd=Decimal("40")) == []
    assert errors(btc_max_price_age_ms=999)
    assert errors(btc_max_price_age_ms=30001)
    assert errors(btc_feed_provider="coinbase")


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("LIVE_BTC_GATE_MODE", "ENFORCE")
    monkeypatch.setenv("LIVE_BTC_GATE_MIN_DELTA_USD", "12.5")
    monkeypatch.setenv("LIVE_BTC_GATE_MAX_DELTA_USD", "80")
    monkeypatch.setenv("LIVE_BTC_MAX_PRICE_AGE_MS", "4000")
    config = LiveConfig.from_env()
    assert config.btc_gate_mode == "enforce"
    assert config.btc_gate_min_delta_usd == Decimal("12.5")
    assert config.btc_gate_max_delta_usd == Decimal("80")
    assert config.btc_max_price_age_ms == 4000
    monkeypatch.setenv("LIVE_BTC_GATE_MAX_DELTA_USD", "")
    assert LiveConfig.from_env().btc_gate_max_delta_usd is None
