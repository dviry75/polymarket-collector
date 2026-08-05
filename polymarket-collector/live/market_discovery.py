from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .repository import LiveRepository, now_iso


EXPECTED_SERIES_SLUG = "btc-up-or-down-5m"
EXPECTED_SLUG_PREFIX = "btc-updown-5m-"


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        result = value.model_dump(mode="json")
        return result if isinstance(result, dict) else {}
    return value if isinstance(value, dict) else {}


def _outcomes(market: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    outcomes = market.get("outcomes") or {}
    if not isinstance(outcomes, dict):
        return None, None, False
    yes = outcomes.get("yes") or {}
    no = outcomes.get("no") or {}
    yes_id = str(yes.get("token_id") or "") or None
    no_id = str(no.get("token_id") or "") or None
    labels = {str(yes.get("label") or "").lower(), str(no.get("label") or "").lower()}
    return yes_id, no_id, bool(yes_id and no_id and labels == {"up", "down"})


async def refresh_btc_5m_markets(repo: LiveRepository) -> list[str]:
    """Discover only the official BTC Up/Down 5m series through the unified SDK.

    The Gamma event supplies series/schedule metadata; both CLOB books independently
    confirm the condition, token IDs and dynamic tick/minimum constraints.
    """
    from polymarket import AsyncPublicClient

    base = (int(datetime.now(timezone.utc).timestamp()) // 300) * 300
    slugs = [f"{EXPECTED_SLUG_PREFIX}{base}", f"{EXPECTED_SLUG_PREFIX}{base + 300}"]
    found: list[str] = []
    client = AsyncPublicClient()
    try:
        for slug in slugs:
            try:
                event = _dump(await client.get_event(slug=slug))
                markets = event.get("markets") or []
                if not markets or event.get("slug") != slug:
                    continue
                series = event.get("series") or []
                series_slugs = {
                    str(item.get("slug") or "") for item in series if isinstance(item, dict)
                }
                market = markets[0] if isinstance(markets[0], dict) else {}
                yes_id, no_id, outcome_mapping_ok = _outcomes(market)
                condition_id = str(market.get("condition_id") or "")
                if not condition_id or not yes_id or not no_id:
                    continue
                books = [
                    _dump(await client.get_order_book(token_id=yes_id)),
                    _dump(await client.get_order_book(token_id=no_id)),
                ]
                book_tokens = {str(book.get("token_id") or book.get("asset_id") or "") for book in books}
                book_conditions = {str(book.get("market") or "") for book in books}
                tick_sizes = {str(book.get("tick_size") or "") for book in books}
                min_sizes = {str(book.get("min_order_size") or "") for book in books}
                scope_verified = (
                    series_slugs == {EXPECTED_SERIES_SLUG}
                    and slug.startswith(EXPECTED_SLUG_PREFIX)
                    and outcome_mapping_ok
                    and book_tokens == {yes_id, no_id}
                    and book_conditions == {condition_id}
                    and len(tick_sizes) == 1
                    and len(min_sizes) == 1
                )
                state = market.get("state") or {}
                trading = market.get("trading") or {}
                fee_schedule = trading.get("fee_schedule") or {}
                schedule = event.get("schedule") or {}
                min_order = next(iter(min_sizes)) if len(min_sizes) == 1 else trading.get("minimum_order_size")
                tick_size = next(iter(tick_sizes)) if len(tick_sizes) == 1 else trading.get("minimum_tick_size")
                fee_rate = fee_schedule.get("rate")
                accepting = state.get("accepting_orders") is True and state.get("closed") is not True
                repo.upsert_market({
                    "event_id": slug,
                    "condition_id": condition_id,
                    "yes_token_id": yes_id,
                    "no_token_id": no_id,
                    "gamma_yes_token_id": yes_id,
                    "gamma_no_token_id": no_id,
                    "token_mapping_status": "verified" if scope_verified else "unverified",
                    "min_order_size": str(min_order) if min_order not in (None, "") else None,
                    "min_tick_size": str(tick_size) if tick_size not in (None, "") else None,
                    "maker_base_fee": "0",
                    "taker_base_fee": str(fee_rate) if fee_rate not in (None, "") else None,
                    "fee_details": {
                        "fee_type": trading.get("fee_type"),
                        "fees_enabled": trading.get("fees_enabled"),
                        "rate": fee_rate,
                        "exponent": fee_schedule.get("exponent"),
                        "rebate_rate": fee_schedule.get("rebate_rate"),
                        "taker_only": fee_schedule.get("taker_only"),
                    },
                    "rfq_enabled": False,
                    "accepting_orders": accepting,
                    "market_resolved": state.get("closed") is True,
                    "source": "polymarket-client-0.1.0b21",
                    "last_update_at": now_iso(),
                    "raw_market_info": {
                        "slug": slug,
                        "title": event.get("title"),
                        "question": market.get("question"),
                        "series_slugs": sorted(series_slugs),
                        "scope_verified": scope_verified,
                        "start_time": schedule.get("start_time"),
                        "end_date": state.get("end_date") or schedule.get("end_date"),
                        "accepting_orders": accepting,
                        "enable_order_book": state.get("enable_order_book"),
                        "restricted_market_metadata": (event.get("state") or {}).get("restricted"),
                        "seconds_delay": trading.get("seconds_delay"),
                        "market_type": "binary_up_down_5m",
                        "neg_risk": (event.get("trading") or {}).get("neg_risk"),
                        "fee_type": trading.get("fee_type"),
                    },
                    "raw_orderbook": {
                        "validated_tokens": sorted(book_tokens),
                        "validated_condition_ids": sorted(book_conditions),
                        "tick_sizes": sorted(tick_sizes),
                        "minimum_order_sizes": sorted(min_sizes),
                    },
                })
                if scope_verified:
                    found.append(condition_id)
                else:
                    repo.audit("market_discovery", "market_scope_validation", "blocked", "METADATA_OR_CLOB_MISMATCH", {"event_id": slug, "condition_id": condition_id})
            except Exception as exc:
                repo.audit("market_discovery", "market_discovery_refresh", "error", f"{type(exc).__name__}: {exc}"[:500], {"event_id": slug})
    finally:
        await client.close()
    return found
