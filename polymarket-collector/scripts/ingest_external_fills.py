#!/usr/bin/env python3
"""Ingest Polymarket's own fill history for our proxy wallet into ``external_fills``.

Dashboard-only, analytics-only. Read-only against Polymarket (HTTP GET only) and
write-only against the two ``external_fills*`` tables -- it never touches the
trader's tables, never signs an order, and never loads the L1 private key.

Auth is **L2 HMAC only** (``POLYMARKET_API_SECRET`` from the environment /
``trader.env``); it does not use ``py-clob-client``, the SDK, or GCP Secret
Manager, so the current GCP billing outage does not block it.

Provenance: ``dec`` / ``load_env_file`` / ``resolve_config`` / ``fetch_data_trades``
and the maker-perspective + fee logic in ``build_fill_rows`` are adapted from
``tools/export_trade_history_simple.py`` in the pre-live holding snapshot
(``/opt/polymarket-btc-live/holding/pre-live-20260830T212534Z/``) -- those
export scripts were never committed to git. The CLOB L2-HMAC request shape
matches ``tools/export_trade_history.py``.

Usage:
    python -m scripts.ingest_external_fills --once        # incremental (systemd timer)
    python -m scripts.ingest_external_fills --backfill     # full history sweep
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live.repository import LiveRepository  # noqa: E402

DEFAULT_ENV_FILE = "/etc/polymarket-live/trader.env"
DEFAULT_DB = "/opt/polymarket-btc-live/poly_live.sqlite3"
CLOB_PATH = "/data/trades"
OVERLAP_SECONDS = 3600
STOP_AFTER_STALE_PAGES = 2
INCREMENTAL_PAGE_CAP = 200
BACKFILL_PAGE_CAP = 10_000

log = logging.getLogger("ingest_external_fills")


# --------------------------------------------------------------------------- #
# config (adapted from tools/export_trade_history_simple.py:51-96)
# --------------------------------------------------------------------------- #
def dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def load_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_config(env_file: str) -> dict[str, str]:
    try:
        file_values = load_env_file(env_file)
    except (FileNotFoundError, PermissionError):
        file_values = {}

    def pick(name: str, default: str = "") -> str:
        return os.environ.get(name) or file_values.get(name) or default

    config = {
        "api_key": pick("POLYMARKET_API_KEY"),
        "api_secret": pick("POLYMARKET_API_SECRET"),
        "api_passphrase": pick("POLYMARKET_API_PASSPHRASE"),
        "signer_address": pick("POLYMARKET_SIGNER_ADDRESS"),
        "proxy_wallet": pick("POLYMARKET_FUNDER_ADDRESS", pick("POLYMARKET_PROFILE_ADDRESS")),
        "clob_host": pick("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").rstrip("/"),
        "data_host": pick("POLYMARKET_DATA_API_HOST", "https://data-api.polymarket.com").rstrip("/"),
    }
    missing = [k for k, v in config.items() if not v]
    if missing:
        raise SystemExit(f"missing configuration: {', '.join(missing)}")
    return config


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #
def to_iso_utc(value: Any) -> str | None:
    """Accept an epoch (seconds, int/str/float) or an ISO string -> ISO-8601 UTC."""
    if value is None or value == "":
        return None
    number = dec(value)
    if number is not None and abs(number) > 10_000_000:  # clearly an epoch, not a small ISO fragment
        try:
            return datetime.fromtimestamp(float(number), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def matched_at_of(trade: dict[str, Any]) -> str | None:
    return to_iso_utc(trade.get("match_time")) or to_iso_utc(trade.get("last_update"))


# --------------------------------------------------------------------------- #
# CLOB fetch -- L2 HMAC, cursor pagination (shape from export_trade_history.py)
# --------------------------------------------------------------------------- #
def _l2_headers(config: dict[str, str]) -> dict[str, str]:
    timestamp = int(time.time())
    secret = config["api_secret"]
    raw = base64.urlsafe_b64decode(secret + "=" * ((-len(secret)) % 4))
    digest = hmac.new(raw, f"{timestamp}GET{CLOB_PATH}".encode(), hashlib.sha256).digest()
    return {
        "POLY_ADDRESS": config["signer_address"],
        "POLY_API_KEY": config["api_key"],
        "POLY_PASSPHRASE": config["api_passphrase"],
        "POLY_SIGNATURE": base64.urlsafe_b64encode(digest).decode("ascii"),
        "POLY_TIMESTAMP": str(timestamp),
    }


def fetch_clob_incremental(
    config: dict[str, str],
    *,
    watermark_iso: str | None,
    full: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Return (trades, truncated). Pages newest-first until the cursor sentinel.

    In incremental mode, stop early once STOP_AFTER_STALE_PAGES consecutive pages
    are entirely older than (watermark - OVERLAP_SECONDS). ``truncated`` is True
    when the page cap was hit before the natural end -- the caller then must NOT
    advance the watermark (mirrors live/trade_window.py).
    """
    floor: datetime | None = None
    if not full and watermark_iso:
        anchor = to_iso_utc(watermark_iso)
        if anchor:
            floor = datetime.fromisoformat(anchor) - timedelta(seconds=OVERLAP_SECONDS)
    page_cap = BACKFILL_PAGE_CAP if full else INCREMENTAL_PAGE_CAP

    trades: list[dict[str, Any]] = []
    cursor: str | None = None
    stale_pages = 0
    pages = 0
    truncated = False
    with httpx.Client(timeout=45) as client:
        while True:
            response = client.get(
                config["clob_host"] + CLOB_PATH,
                params={"next_cursor": cursor} if cursor else {},
                headers=_l2_headers(config),
            )
            response.raise_for_status()
            payload = response.json()
            page = payload.get("data") or []
            trades.extend(page)
            pages += 1
            log.info("clob page=%d rows=%d total=%d cursor=%s", pages, len(page), len(trades), cursor or "-")

            if floor is not None and page:
                stamps = [datetime.fromisoformat(m) for m in (matched_at_of(t) for t in page) if m]
                if stamps and max(stamps) < floor:
                    stale_pages += 1
                    if stale_pages >= STOP_AFTER_STALE_PAGES:
                        break
                else:
                    stale_pages = 0

            nxt = payload.get("next_cursor")
            if not nxt or nxt == "LTE=" or nxt == cursor:
                break
            cursor = nxt
            if pages >= page_cap:
                truncated = True
                log.warning("clob page cap %d reached; run marked truncated", page_cap)
                break
    return trades, truncated


def fetch_data_trades(config: dict[str, str]) -> list[dict[str, Any]]:
    """Public, unauthenticated enrichment feed (event slug / title)."""
    rows: list[dict[str, Any]] = []
    offset, size = 0, 500
    with httpx.Client(timeout=45) as client:
        while True:
            response = client.get(
                config["data_host"] + "/trades",
                params={"user": config["proxy_wallet"], "takerOnly": "false", "limit": size, "offset": offset},
            )
            response.raise_for_status()
            page = response.json()
            rows.extend(page)
            if len(page) < size:
                break
            offset += len(page)
    return rows


# --------------------------------------------------------------------------- #
# row building -- account perspective + calculated fee
# (adapted from tools/export_trade_history_simple.py:149-231)
# --------------------------------------------------------------------------- #
def build_fill_rows(
    clob: list[dict[str, Any]],
    data: list[dict[str, Any]],
    proxy: str,
) -> list[dict[str, Any]]:
    by_tx: dict[str, list[dict[str, Any]]] = {}
    for row in data:
        by_tx.setdefault(str(row.get("transactionHash", "")).lower(), []).append(row)

    ingested_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    for trade in clob:
        trade_id = trade.get("id")
        matched_at = matched_at_of(trade)
        if not trade_id or not matched_at:
            log.warning("skipping trade with no id/timestamp: %s", str(trade.get("id")))
            continue

        trader_side = str(trade.get("trader_side") or "").upper() or None
        mine = [
            o for o in (trade.get("maker_orders") or [])
            if isinstance(o, dict) and str(o.get("maker_address", "")).lower() == proxy
        ]
        if trader_side == "MAKER" and mine:
            size = sum((dec(o.get("matched_amount")) or Decimal(0) for o in mine), Decimal(0))
            weighted = sum(
                ((dec(o.get("price")) or Decimal(0)) * (dec(o.get("matched_amount")) or Decimal(0)) for o in mine),
                Decimal(0),
            )
            price = (weighted / size) if size > 0 else dec(mine[0].get("price"))
            side = str(mine[0].get("side") or "").upper()
            outcome = str(mine[0].get("outcome") or "")
        else:
            size, price = dec(trade.get("size")), dec(trade.get("price"))
            side = str(trade.get("side") or "").upper()
            outcome = str(trade.get("outcome") or "")

        if side not in ("BUY", "SELL") or price is None or size is None:
            log.warning("skipping trade %s: unresolved side/price/size", trade_id)
            continue

        rate_bps = dec(trade.get("fee_rate_bps"))
        if rate_bps is None:
            fee_usd = None
        elif rate_bps == 0:
            fee_usd = 0.0
        else:
            fee_usd = float(
                (size * price * (Decimal(1) - price) * rate_bps / Decimal(10000)).quantize(Decimal("0.000001"))
            )

        tx_hash = str(trade.get("transaction_hash") or "")
        candidates = by_tx.get(tx_hash.lower(), [])
        match = candidates[0] if len(candidates) == 1 else None
        if match is None and len(candidates) > 1:
            exact = [c for c in candidates if str(c.get("side", "")).upper() == side and dec(c.get("size")) == size]
            match = exact[0] if len(exact) == 1 else None

        owner = trade.get("owner")
        owner_hash = hashlib.sha256(str(owner).encode()).hexdigest() if owner else None

        rows.append({
            "trade_id": str(trade_id),
            "condition_id": str(trade.get("market") or ""),
            "token_id": str(trade.get("asset_id") or "") or None,
            "event_slug": (match.get("eventSlug") if match else None) or None,
            "market_title": (match.get("title") if match else None) or None,
            "side": side,
            "outcome": (match.get("outcome") if match else outcome) or outcome or None,
            "price": float(price),
            "size": float(size),
            "notional_usd": float(price * size),
            "fee_usd": fee_usd,
            "fee_rate_bps": float(rate_bps) if rate_bps is not None else None,
            "trader_side": trader_side if trader_side in ("TAKER", "MAKER") else None,
            "transaction_hash": tx_hash or None,
            "matched_at": matched_at,
            "status": str(trade.get("status") or "") or None,
            "owner_hash": owner_hash,
            "raw_json": json.dumps({**trade, "owner": owner_hash}, sort_keys=True, ensure_ascii=False),
            "ingested_at": ingested_at,
        })
    return rows


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
_UPSERT = """
INSERT INTO external_fills (
    trade_id, condition_id, token_id, event_slug, market_title, side, outcome,
    price, size, notional_usd, fee_usd, fee_rate_bps, trader_side,
    transaction_hash, matched_at, status, owner_hash, raw_json, ingested_at
) VALUES (
    :trade_id, :condition_id, :token_id, :event_slug, :market_title, :side, :outcome,
    :price, :size, :notional_usd, :fee_usd, :fee_rate_bps, :trader_side,
    :transaction_hash, :matched_at, :status, :owner_hash, :raw_json, :ingested_at
)
ON CONFLICT(trade_id) DO UPDATE SET
    event_slug   = COALESCE(excluded.event_slug, external_fills.event_slug),
    market_title = COALESCE(excluded.market_title, external_fills.market_title),
    outcome      = COALESCE(excluded.outcome, external_fills.outcome),
    status       = excluded.status,
    fee_usd      = excluded.fee_usd,
    fee_rate_bps = excluded.fee_rate_bps,
    raw_json     = excluded.raw_json,
    ingested_at  = excluded.ingested_at
"""


def read_watermark(repo: LiveRepository) -> str | None:
    with repo.connect() as conn:
        row = conn.execute(
            "SELECT value FROM external_fills_sync WHERE key='last_matched_at'"
        ).fetchone()
    return row["value"] if row and row["value"] else None


def persist(repo: LiveRepository, rows: list[dict[str, Any]], sync: dict[str, str]) -> tuple[int, int]:
    """One short RW transaction. Returns (rows_seen, rows_new_estimate)."""
    if not rows and not sync:
        return 0, 0
    with repo.connect() as conn:
        existing: set[str] = set()
        if rows:
            existing = {
                r["trade_id"]
                for r in conn.execute(
                    "SELECT trade_id FROM external_fills WHERE trade_id IN (%s)"
                    % ",".join("?" * len(rows)),
                    [r["trade_id"] for r in rows],
                ).fetchall()
            }
            conn.executemany(_UPSERT, rows)
        if sync:
            conn.executemany(
                "INSERT INTO external_fills_sync(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                list(sync.items()),
            )
        conn.commit()
    new = sum(1 for r in rows if r["trade_id"] not in existing)
    return len(rows), new


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def resolve_floor(repo: LiveRepository, override: str | None) -> str | None:
    if override:
        return to_iso_utc(override)
    env_floor = os.environ.get("EXTERNAL_FILLS_SINCE")
    if env_floor:
        return to_iso_utc(env_floor)
    stored = repo.get_state("dashboard_cutover_at", "")
    return to_iso_utc(stored) if stored else None


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s ingest_external_fills %(message)s",
    )
    config = resolve_config(args.env_file)
    db_path = Path(args.db)
    if not db_path.is_file():
        log.error("database not found: %s (run scripts.migrate_external_fills first)", db_path)
        return 1
    repo = LiveRepository(db_path)  # read-write

    floor = resolve_floor(repo, args.since)
    watermark = None if args.backfill else read_watermark(repo)
    run_at = datetime.now(timezone.utc).isoformat()
    log.info("start mode=%s watermark=%s floor=%s", "backfill" if args.backfill else "incremental", watermark, floor)

    try:
        clob, truncated = fetch_clob_incremental(config, watermark_iso=watermark, full=bool(args.backfill))
        try:
            data = fetch_data_trades(config)
            enriched_available = len(data)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("public enrichment feed unavailable (%s); continuing without event slugs", exc)
            data, enriched_available = [], 0

        rows = build_fill_rows(clob, data, config["proxy_wallet"].lower())
        max_matched = max((r["matched_at"] for r in rows), default=None)

        sync: dict[str, str] = {
            "last_run_at": run_at,
            "last_run_status": "ok",
            "last_run_rows": str(len(rows)),
            "last_error": "",
        }
        # advance the watermark only on a clean (non-truncated) run
        if not truncated and max_matched:
            prior = watermark or ""
            sync["last_matched_at"] = max(max_matched, prior) if prior else max_matched
        if args.backfill and not truncated:
            sync["backfill_completed_at"] = run_at

        seen, new = persist(repo, rows, sync)
        log.info(
            "done fetched=%d new=%d updated=%d clob_trades=%d enrichment_rows=%d truncated=%s watermark=%s",
            seen, new, seen - new, len(clob), enriched_available, truncated,
            sync.get("last_matched_at", watermark),
        )
        return 0
    except httpx.HTTPStatusError as exc:
        log.error("HTTP %s from %s: %s", exc.response.status_code, exc.request.url, exc.response.text[:300])
        persist(repo, [], {"last_run_at": run_at, "last_run_status": "error", "last_error": f"http {exc.response.status_code}"})
        return 1
    except Exception as exc:  # noqa: BLE001 -- top-level guard, re-raised as exit code
        log.exception("ingestion failed: %s", exc)
        try:
            persist(repo, [], {"last_run_at": run_at, "last_run_status": "error", "last_error": str(exc)[:300]})
        except Exception:  # noqa: BLE001
            pass
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="single pass then exit (default behaviour)")
    parser.add_argument("--backfill", action="store_true", help="ignore the watermark; sweep full history")
    parser.add_argument("--since", help="ISO timestamp floor for this run (overrides EXTERNAL_FILLS_SINCE / cutover)")
    parser.add_argument("--db", default=os.environ.get("LIVE_DB_PATH") or DEFAULT_DB)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
