"""Data API v2 client: cursor walk, v1 field aliases and every v2 caller.

No network: httpx.MockTransport serves v2-shaped pages.
"""

import asyncio
from decimal import Decimal
import json

import httpx
import pytest

import live.account_identity as account_identity_module
import live.adapters.polymarket as polymarket_module
from live.account_identity import PublicAccountIdentityClient
from live.adapters.polymarket import RealPolymarketTradingAdapter
from live.config import LiveConfig
from live.data_api import fetch_all, v2_url, with_v1_aliases
from live.trader_commands import select_verified_redemption

WALLET = "0x" + "ab" * 20
CONDITION = "0x" + "cd" * 32
TX = "0x" + "ef" * 32

POSITION = {
    "proxy_wallet": WALLET, "condition_id": CONDITION, "token_id": "111",
    "outcome": "Up", "status": "REDEEMABLE", "current_size": 3.68,
    "avg_price": 0.74, "current_value": 0, "redeemable": True,
}
REDEEM = {
    "type": "REDEEM", "proxy_wallet": WALLET, "condition_id": CONDITION,
    "token_id": "111", "size": 5.0, "usdc_size": 5.0, "timestamp": 1789167967,
    "transaction_hash": TX, "event_slug": "btc-updown-5m-1", "title": "BTC Up or Down",
}


def _transport(pages_by_path, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        pages = pages_by_path[request.url.path]
        index = int(request.url.params.get("cursor") or 0)
        nxt = str(index + 1) if index + 1 < len(pages) else None
        body = {"data": pages[index], "pagination": {"has_more": nxt is not None, "next_cursor": nxt}}
        return httpx.Response(200, text=json.dumps(body))
    return httpx.MockTransport(handler)


def _patch_async_client(monkeypatch, module, transport):
    real = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))


def test_v2_url_and_v1_aliases():
    assert v2_url("https://data-api.polymarket.com/", "/positions") == "https://data-api.polymarket.com/v2/positions"
    assert v2_url("https://data-api.polymarket.com/v2", "trades") == "https://data-api.polymarket.com/v2/trades"
    row = with_v1_aliases(POSITION)
    assert row["conditionId"] == CONDITION and row["asset"] == "111"
    assert row["proxyWallet"] == WALLET and row["size"] == 3.68 and row["currentValue"] == 0
    activity = with_v1_aliases(REDEEM)
    assert activity["transactionHash"] == TX and activity["usdcSize"] == 5.0
    assert activity["eventSlug"] == "btc-updown-5m-1"
    assert activity["size"] == 5.0  # activity already has `size`; never overwritten


def test_cursor_walk_resends_filters_without_limit_or_offset():
    seen = []
    transport = _transport({"/v2/trades": [[{"transaction_hash": "a"}], [{"transaction_hash": "b"}], []]}, seen)
    with httpx.Client(transport=transport) as client:
        rows = fetch_all(client, "https://h", "/trades", {"user": WALLET, "taker_only": "false", "limit": 1000})
    assert [r["transactionHash"] for r in rows] == ["a", "b"]
    assert seen[0][1] == {"user": WALLET, "taker_only": "false", "limit": "1000"}
    assert seen[1][1] == {"user": WALLET, "taker_only": "false", "cursor": "1"}
    assert all("offset" not in params for _, params in seen)


def test_page_cap_raises_unless_truncating():
    seen = []
    transport = _transport({"/v2/trades": [[{"x": 1}], [{"x": 2}]]}, seen)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(RuntimeError, match="exceeded 1 pages"):
            fetch_all(client, "https://h", "/trades", {"user": WALLET}, max_pages=1)
        assert len(fetch_all(client, "https://h", "/trades", {"user": WALLET}, max_pages=1, truncate=True)) == 1


def test_null_data_is_an_empty_page_and_errors_raise():
    def handler(request):
        if request.url.path == "/v2/value":
            return httpx.Response(200, text='{"data": null}')
        return httpx.Response(410, text='{"error": "Data API v1 has been retired"}')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_all(client, "https://h", "/value", {"user": WALLET}) == []
        with pytest.raises(httpx.HTTPStatusError):
            fetch_all(client, "https://h", "/positions", {"user": WALLET})


def test_adapter_positions_come_from_v2_with_exact_decimals(monkeypatch):
    seen = []
    second = {**POSITION, "token_id": "222", "current_size": 0.1000001}
    _patch_async_client(monkeypatch, polymarket_module, _transport({"/v2/positions": [[POSITION], [second]]}, seen))
    config = LiveConfig(live_adapter="polymarket", funder_address=WALLET)
    adapter = RealPolymarketTradingAdapter(config, secure_client=object())
    positions = asyncio.run(adapter.get_positions())
    assert [p["token_id"] for p in positions] == ["111", "222"]
    first = positions[0]
    assert first["condition_id"] == CONDITION and first["outcome"] == "Up"
    assert first["size"] == "3.68" and Decimal(first["size"]) == Decimal("3.68")
    assert positions[1]["size"] == "0.1000001"
    assert first["redeemable"] is True and first["orphan"] is True
    assert seen[0] == ("/v2/positions", {"user": WALLET, "limit": "500"})


def test_identity_resolve_uses_v2_positions_and_trades(monkeypatch):
    seen = []
    pages = {
        "/v2/positions": [[{**POSITION, "current_value": 1.5}]],
        "/v2/trades": [[{"transaction_hash": "a"}], [{"transaction_hash": "b"}]],
    }
    _patch_async_client(monkeypatch, account_identity_module, _transport(pages, seen))
    result = asyncio.run(PublicAccountIdentityClient("https://h").resolve(WALLET))
    assert result.resolved_proxy_wallet == WALLET
    assert result.public_positions_count == 1 and result.public_positions_value == 1.5
    assert result.public_closed_positions_count == 1
    assert result.public_activity_count == 1  # sample: first trades page only
    assert ("/v2/positions", {"user": WALLET, "limit": "500", "status": "REDEEMABLE"}) in seen


def test_redemption_proof_still_selects_from_v2_activity(monkeypatch):
    seen = []
    _patch_async_client(monkeypatch, account_identity_module, _transport({"/v2/activity": [[REDEEM]]}, seen))
    activity = asyncio.run(PublicAccountIdentityClient("https://h").redemption_activity(WALLET, CONDITION))
    assert seen[0][1] == {"user": WALLET, "condition": CONDITION, "type": "REDEEM",
                          "limit": "100", "sort_direction": "DESC"}
    proof = select_verified_redemption(
        activity, wallet=WALLET, condition_id=CONDITION, token_id="111",
        remaining_shares=Decimal("5"),
    )
    assert proof is not None and proof["transactionHash"] == TX
    assert select_verified_redemption(
        activity, wallet=WALLET, condition_id=CONDITION, token_id="999",
        remaining_shares=Decimal("5"),
    ) is None


def test_ingest_and_export_fetchers_walk_v2(monkeypatch):
    import scripts.ingest_external_fills as ingest
    import tools.export_trade_history_simple as export

    seen = []
    transport = _transport({"/v2/activity": [[REDEEM]], "/v2/trades": [[{"transaction_hash": TX}]]}, seen)
    real = httpx.Client
    for module in (ingest, export):
        monkeypatch.setattr(module.httpx, "Client", lambda **kw: real(transport=transport, **kw))
    config = {"data_host": "https://h", "proxy_wallet": WALLET}
    redemptions = ingest.fetch_redemptions(config)
    assert redemptions[0]["conditionId"] == CONDITION and redemptions[0]["usdcSize"] == 5.0
    assert ingest.fetch_data_trades(config)[0]["transactionHash"] == TX
    assert export.fetch_data_trades(config)[0]["transactionHash"] == TX
    assert ("/v2/activity", {"user": WALLET, "type": "REDEEM", "start": "1", "limit": "1000",
                             "sort_direction": "DESC"}) in seen
    assert ("/v2/trades", {"user": WALLET, "taker_only": "false", "start": "1", "limit": "1000"}) in seen
