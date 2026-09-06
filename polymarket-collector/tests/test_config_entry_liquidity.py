import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.config import LiveConfig, _decimal_tuple_env


def _errs(**env):
    import os
    saved = {}
    keys = [
        "LIVE_ENTRY_LIQUIDITY_CAPTURE_ENABLED",
        "LIVE_ENTRY_LIQUIDITY_BUCKET_LEVELS",
        "LIVE_ENTRY_LIQUIDITY_BUFFER_MULTIPLES",
        "LIVE_ENTRY_LIQUIDITY_DEEP_CAPTURE_MAX_VWAP",
    ]
    for k in keys:
        saved[k] = os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    try:
        cfg = LiveConfig.from_env()
        return cfg, [e for e in cfg.validation_errors() if "ENTRY_LIQUIDITY" in e]
    finally:
        for k in keys:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


def test_defaults_are_valid_and_capture_on():
    cfg, errs = _errs()
    assert cfg.entry_liquidity_capture_enabled is True
    assert cfg.entry_liquidity_bucket_levels == (
        Decimal("0.66"), Decimal("0.60"), Decimal("0.55"), Decimal("0.46"),
    )
    assert cfg.entry_liquidity_buffer_multiples == (
        Decimal("3"), Decimal("4"), Decimal("5"),
    )
    assert errs == []


def test_csv_env_overrides_parse():
    cfg, errs = _errs(
        LIVE_ENTRY_LIQUIDITY_BUCKET_LEVELS="0.70, 0.55",
        LIVE_ENTRY_LIQUIDITY_BUFFER_MULTIPLES="2,10",
    )
    assert cfg.entry_liquidity_bucket_levels == (Decimal("0.70"), Decimal("0.55"))
    assert cfg.entry_liquidity_buffer_multiples == (Decimal("2"), Decimal("10"))
    assert errs == []


def test_non_descending_buckets_rejected():
    _, errs = _errs(LIVE_ENTRY_LIQUIDITY_BUCKET_LEVELS="0.5,0.6")
    assert any("BUCKET_LEVELS" in e for e in errs)


def test_multiple_not_greater_than_one_rejected():
    _, errs = _errs(LIVE_ENTRY_LIQUIDITY_BUFFER_MULTIPLES="1,2")
    assert any("BUFFER_MULTIPLES" in e for e in errs)


def test_deep_capture_vwap_out_of_range_rejected():
    _, errs = _errs(LIVE_ENTRY_LIQUIDITY_DEEP_CAPTURE_MAX_VWAP="1.4")
    assert any("DEEP_CAPTURE_MAX_VWAP" in e for e in errs)


def test_decimal_tuple_env_falls_back_on_junk():
    assert _decimal_tuple_env("NOPE_MISSING_X", "3,4,5") == (
        Decimal("3"), Decimal("4"), Decimal("5"),
    )


def test_summary_exposes_entry_liquidity():
    cfg, _ = _errs()
    summary = cfg.safe_public_dict()
    assert summary["entry_liquidity_bucket_levels"] == "0.66,0.60,0.55,0.46"
    assert summary["entry_liquidity_capture_enabled"] is True
