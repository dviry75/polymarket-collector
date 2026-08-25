from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from live.config import LiveConfig
from live.dashboard_api import configure_dashboard_api, router
from live.dashboard_read_model import DashboardQueryError, DashboardReadModel, resolve_window
from live.dashboard_schema import mark_reconciled_provenance, migrate_dashboard_schema
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository


CUTOVER = "2026-08-12T10:00:00+00:00"
NOW = datetime.fromisoformat("2026-08-12T10:10:01+00:00")


def build_db(tmp: str) -> tuple[LiveRepository, LiveConfig]:
    repo = LiveRepository(Path(tmp) / "dashboard.sqlite3")
    repo.migrate()
    StrategyRepository(repo).migrate()
    config = LiveConfig(
        trading_mode="LIVE", execution_mode="REAL_TRADING", environment="LIVE",
        strategy_id="btc-updown-5m", strategy_version="test-v1",
        provenance_source="TEST", live_db_path=str(repo.db_path),
        login_username="Admin@system.com",
        login_password_hash="sha256:" + hashlib.sha256(b"pw").hexdigest(),
        session_secret="test-session-secret",
    )
    migrate_dashboard_schema(repo, config, cutover_at=CUTOVER, run_id="test-run")
    return repo, config


def seed_verified_lifecycle(repo: LiveRepository) -> None:
    repo.store_account_snapshot({
        "sampled_at": "2026-08-12T10:10:00+00:00", "account_identity_status": "VERIFIED",
        "public_positions_count": 1, "public_positions_value": 3.2,
        "balance_usd": 100, "status": "ok",
    })
    with repo.connect() as conn:
        conn.execute("""INSERT INTO live_reconciliation_runs(started_at,finished_at,status,gaps_count)
                        VALUES('2026-08-12T10:09:59+00:00','2026-08-12T10:10:00+00:00','ok',0)""")
        conn.execute("""INSERT INTO live_event_states(event_id,condition_id,status,locked_side,locked_token_id,lock_reason,entry_intent_id,locked_at,updated_at)
                        VALUES('btc-updown-5m-1786529400','condition','OPEN','YES','token','TEST','intent','2026-08-12T10:00:01+00:00','2026-08-12T10:00:01+00:00')""")
        conn.execute("""INSERT INTO live_strategy_intents(intent_id,correlation_id,event_id,condition_id,position_id,action,purpose,token_id,side,state,order_type,requested_amount_text,requested_shares_text,price_limit_text,max_spend_text,filled_shares_text,average_price_text,fee_text,remaining_shares_text,remote_order_id,created_at,updated_at)
                        VALUES('intent','corr','btc-updown-5m-1786529400','condition','position','ENTRY','ENTRY','token','YES','PARTIAL','FAK','5','10','0.5','5','4','0.5','0.2','6','remote-order','2026-08-12T10:00:02+00:00','2026-08-12T10:00:02+00:00')""")
        conn.execute("""INSERT INTO live_strategy_fills(fill_id,intent_id,remote_trade_id,shares_text,price_text,fee_text,fee_verification_status,fee_source,status,matched_at,created_at,updated_at)
                        VALUES('fill','intent','remote-trade','4','0.5','0.2','VERIFIED','test_formula','MATCHED','2026-08-12T10:00:03+00:00','2026-08-12T10:00:03+00:00','2026-08-12T10:00:03+00:00')""")
        conn.execute("""INSERT INTO live_strategy_positions(position_id,event_id,condition_id,token_id,outcome,state,acquired_shares_text,remaining_shares_text,sellable_shares_text,average_entry_price_text,cost_all_in_text,entry_fees_text,created_at,updated_at)
                        VALUES('position','btc-updown-5m-1786529400','condition','token','YES','OPEN','10','4','4','1','10','0.2','2026-08-12T10:00:04+00:00','2026-08-12T10:00:04+00:00')""")
        conn.execute("""INSERT INTO live_strategy_deals(deal_id,event_id,position_id,state,outcome,total_fees_text,realized_pnl_text,fee_verification_status,fee_source,final_reason,opened_at,closed_at,created_at,updated_at)
                        VALUES('deal','btc-updown-5m-1786529400','position','CLOSED','YES','0.5','2','VERIFIED','adapter_fill_fees','TAKE_PROFIT','2026-08-12T10:00:04+00:00','2026-08-12T10:05:00+00:00','2026-08-12T10:00:04+00:00','2026-08-12T10:05:00+00:00')""")
        conn.execute("""INSERT INTO live_market_snapshots(condition_id,event_id,asset_id,outcome,event_type,best_bid,best_ask,market_timestamp,received_at,source,message_hash)
                        VALUES('condition','btc-updown-5m-1786529400','token','YES','book','0.8','0.81','2026-08-12T10:10:00+00:00','2026-08-12T10:10:00+00:00','TEST','hash')""")
        conn.commit()
    mark_reconciled_provenance(repo)


def test_migration_is_idempotent_and_does_not_invent_legacy_provenance():
    with tempfile.TemporaryDirectory() as tmp:
        repo = LiveRepository(Path(tmp) / "db.sqlite3")
        repo.migrate(); strategy = StrategyRepository(repo); strategy.migrate()
        with repo.connect() as conn:
            conn.execute("""INSERT INTO live_strategy_positions(position_id,event_id,condition_id,token_id,outcome,state,acquired_shares_text,remaining_shares_text,sellable_shares_text,average_entry_price_text,cost_all_in_text,created_at,updated_at)
                            VALUES('legacy','legacy-event','c','t','YES','CLOSED','1','0','0','0.5','0.5','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')""")
            conn.commit()
        config = LiveConfig(execution_mode="REAL_TRADING", environment="LIVE")
        first = migrate_dashboard_schema(repo, config, cutover_at=CUTOVER, run_id="run-one")
        second = migrate_dashboard_schema(repo, config, run_id="run-two")
        assert first.cutover_at == second.cutover_at == CUTOVER
        with repo.connect() as conn:
            legacy = conn.execute("SELECT execution_mode,environment,verification_status FROM live_strategy_positions WHERE position_id='legacy'").fetchone()
            migrations = conn.execute("SELECT count(*) FROM live_schema_migrations").fetchone()[0]
        assert tuple(legacy) == ("UNKNOWN", "UNKNOWN", "UNKNOWN")
        assert migrations == 6
        with repo.connect() as conn:
            conn.execute("UPDATE live_strategy_positions SET updated_at=? WHERE position_id='legacy'", (NOW.isoformat(),))
            updated = conn.execute("SELECT execution_mode,environment,run_id FROM live_strategy_positions WHERE position_id='legacy'").fetchone()
            conn.commit()
        assert tuple(updated) == ("REAL_TRADING", "LIVE", "run-one")


def test_future_writes_get_explicit_provenance_and_missing_context_fails_unknown():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp)
        repo.store_account_snapshot({"sampled_at": NOW.isoformat(), "account_identity_status": "VERIFIED", "status": "ok"})
        with repo.connect() as conn:
            row = conn.execute("SELECT execution_mode,environment,run_id,verification_status FROM live_account_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            conn.execute("DELETE FROM live_system_state WHERE key LIKE 'provenance_%'")
            conn.execute("INSERT INTO live_account_snapshots(sampled_at,status) VALUES(?,?)", (NOW.isoformat(), "unknown"))
            missing = conn.execute("SELECT execution_mode,environment,run_id FROM live_account_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            conn.commit()
        assert tuple(row) == ("REAL_TRADING", "LIVE", "test-run", "VERIFIED")
        assert tuple(missing) == ("UNKNOWN", "UNKNOWN", "UNKNOWN")


def test_partial_fill_equity_and_pnl_are_fill_based_without_double_counting():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp); seed_verified_lifecycle(repo)
        model = DashboardReadModel(LiveRepository(repo.db_path, query_only=True))
        equity = model.account_equity(now=NOW)
        position = equity["items"][0]
        assert position["conservative_value_usd"] == 3.2
        assert position["remaining_attributed_cost_usd"] == 4.0
        assert round(position["unrealized_pnl_usd"], 8) == -0.8
        assert equity["reserved"]["value"] == 3.0
        assert equity["claimable"]["value"] == 0.0
        assert equity["total_equity"]["value"] == 106.2
        pnl = model.pnl_summary(resolve_window("today", now=NOW))
        assert pnl["realized_pnl_usd"] == 2.0
        assert pnl["fees_usd"] == 0.5
        assert pnl["trade_count"] == 1
        assert pnl["win_rate_percent"] == 100.0


def test_stale_bid_never_produces_unrealized_value():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp); seed_verified_lifecycle(repo)
        model = DashboardReadModel(LiveRepository(repo.db_path, query_only=True), market_stale_seconds=1)
        items = model.open_positions(now=datetime.fromisoformat("2026-08-12T10:20:00+00:00"))["items"]
        assert items[0]["quality"] == "STALE"
        assert items[0]["conservative_value_usd"] is None
        assert items[0]["unrealized_pnl_usd"] is None


def test_jerusalem_day_boundaries_cover_dst_and_validate_ranges():
    spring = resolve_window("today", now=datetime.fromisoformat("2026-03-27T12:00:00+00:00"))
    assert spring.start_utc.astimezone(timezone.utc) < spring.end_utc.astimezone(timezone.utc)
    assert spring.start_utc.astimezone().tzinfo is not None
    custom = resolve_window("custom", from_date="2026-08-01", to_date="2026-08-03")
    assert (custom.end_utc - custom.start_utc).total_seconds() == 3 * 86400
    try:
        resolve_window("custom", from_date="2026-01-01", to_date="2026-06-01")
        assert False, "range should be bounded"
    except DashboardQueryError:
        pass


def test_dashboard_api_auth_get_only_pagination_and_no_raw_identifiers():
    with tempfile.TemporaryDirectory() as tmp:
        repo, config = build_db(tmp); seed_verified_lifecycle(repo)
        configure_dashboard_api(repo.db_path, config, trader_status_provider=lambda: None)
        app = FastAPI(); app.include_router(router)
        client = TestClient(app, base_url="https://testserver")
        assert client.get("/live/dashboard/v1/overview").status_code == 401
        from live.auth import LiveAuthManager
        token = LiveAuthManager(config).create_session(config.login_username)
        client.cookies.set("live_session", token)
        overview = client.get("/live/dashboard/v1/overview")
        assert overview.status_code == 200
        payload = overview.json()
        assert payload["meta"]["cutover_at"] == CUTOVER
        # The fixture is intentionally historical relative to wall-clock time;
        # the HTTP layer must preserve the read model's fail-closed stale state.
        assert payload["data"]["account"]["cash"]["quality"] == "STALE"
        trades = client.get("/live/dashboard/v1/trades?page=1&page_size=1&range=today")
        assert trades.status_code == 200 and trades.json()["data"]["page_size"] == 1
        assert "remote-order" not in trades.text and "remote-trade" not in trades.text
        assert client.post("/live/dashboard/v1/overview").status_code == 405
        assert client.get("/live/dashboard/v1/trades?page_size=101").status_code == 422


def test_health_exposes_three_tier_recovery_and_operational_state():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp)
        seed_verified_lifecycle(repo)
        repo.set_states({
            "pause_entries": "true",
            "pause_state": "PAUSED_RECOVERING",
            "pause_cause": "RECONCILIATION_TEMPORARY_ERROR",
            "release_policy": "AUTO_WHEN_CLEAN",
            "operator_action_required": "false",
            "operator_action_reason": "",
            "global_entry_halt_required": "true",
            "global_entry_halt_reason": "RECONCILIATION_TEMPORARY_ERROR",
            "incident_scope": "GLOBAL",
            "reconciliation_readiness": "READY",
            "last_successful_reconciliation_at": (
                "2026-08-12T10:10:00+00:00"
            ),
            "auto_repair_last_at": "2026-08-12T10:09:00+00:00",
            "auto_repair_count_24h": "2",
        }, "test")
        with repo.connect() as conn:
            conn.execute(
                "INSERT INTO live_reconciliation_runs"
                "(started_at,status) VALUES(?,?)",
                ("2026-08-12T10:00:00+00:00", "running"),
            )
            conn.execute(
                "INSERT INTO live_quarantines("
                "quarantine_id,incident_scope,entity_type,entity_id,"
                "position_id,reason_code,status,first_seen_at,last_seen_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "q1", "POSITION", "POSITION", "position",
                    "position", "SCOPED_TEST", "OPEN",
                    "2026-08-12T10:05:00+00:00",
                    "2026-08-12T10:06:00+00:00",
                ),
            )
            conn.commit()
        model = DashboardReadModel(
            LiveRepository(repo.db_path, query_only=True)
        )
        health = model.health(
            {
                "recovery": {
                    "stability_elapsed_ms": 2000,
                    "stability_target_ms": 4000,
                }
            },
            now=NOW,
        )
        assert health["trading_status"] == "TRANSIENT_BLOCK"
        assert (
            health["recovery"]["classification"]
            == "TRANSIENT_GLOBAL_BLOCK"
        )
        assert health["operator"] == {
            "action_required": False, "reason": ""
        }
        assert health["global_halt"]["required"] is True
        assert health["incident_scope"] == "GLOBAL"
        assert health["quarantine"]["count"] == 1
        assert health["quarantine"]["items"][0]["age_seconds"] == 301.0
        assert health["reconciliation"]["success_age_seconds"] == 1.0
        assert health["reconciliation"]["running_count"] == 1
        assert health["reconciliation"]["stuck_running_count"] == 1
        assert health["auto_repair"]["count_24h"] == 2
        assert (
            health["recovery"]["stability"]["runtime_target_ms"]
            == 4000
        )


def test_missing_fee_is_partial_and_never_rendered_as_zero():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp); seed_verified_lifecycle(repo)
        with repo.connect() as conn:
            conn.execute("UPDATE live_strategy_deals SET fee_verification_status='UNKNOWN',fee_source=NULL")
            conn.commit()
        model = DashboardReadModel(LiveRepository(repo.db_path, query_only=True))
        result = model.pnl_summary(resolve_window("today", now=NOW))
        assert result["quality"] == "PARTIAL"
        assert result["fees_usd"] is None
        assert result["realized_pnl_usd"] == 2.0


def test_claimable_is_mutually_exclusive_with_open_position_value():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp); seed_verified_lifecycle(repo)
        with repo.connect() as conn:
            conn.execute("UPDATE live_strategy_positions SET state='REDEEM_PENDING',resolved_winner=1")
            conn.commit()
        model = DashboardReadModel(LiveRepository(repo.db_path, query_only=True))
        result = model.account_equity(now=NOW)
        assert result["positions"]["value"] == 0.0
        assert result["claimable"]["value"] == 4.0
        assert result["total_equity"]["value"] == 107.0


def test_paper_and_unknown_rows_are_excluded_from_live_read_model():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp); seed_verified_lifecycle(repo)
        with repo.connect() as conn:
            conn.execute("""INSERT INTO live_strategy_deals(
                deal_id,event_id,state,total_fees_text,realized_pnl_text,fee_verification_status,
                closed_at,created_at,updated_at,execution_mode,environment,run_id,strategy_id,
                strategy_version,provenance_source,ingested_at,verification_status)
                VALUES('paper-deal','paper-event','CLOSED','0','999','VERIFIED',?,?,?,
                'PAPER_TRADING','STAGING','paper-run','s','v','PAPER',?,'VERIFIED')""",
                (NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), NOW.isoformat()))
            conn.execute("DELETE FROM live_system_state WHERE key LIKE 'provenance_%'")
            conn.execute("""INSERT INTO live_strategy_deals(
                deal_id,event_id,state,total_fees_text,realized_pnl_text,fee_verification_status,
                closed_at,created_at,updated_at,execution_mode,environment,run_id,strategy_id,
                strategy_version,provenance_source,ingested_at,verification_status)
                VALUES('unknown-deal','unknown-event','CLOSED','0','999','VERIFIED',?,?,?,
                'UNKNOWN','UNKNOWN','UNKNOWN','UNKNOWN','UNKNOWN','UNKNOWN',?,'VERIFIED')""",
                (NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), NOW.isoformat()))
            conn.commit()
        result = DashboardReadModel(LiveRepository(repo.db_path, query_only=True)).pnl_summary(resolve_window("today", now=NOW))
        assert result["trade_count"] == 1
        assert result["realized_pnl_usd"] == 2.0


def test_dst_days_are_exactly_23_and_25_hours():
    spring = resolve_window("custom", from_date="2026-03-27", to_date="2026-03-27")
    autumn = resolve_window("custom", from_date="2026-10-25", to_date="2026-10-25")
    assert (spring.end_utc - spring.start_utc).total_seconds() == 23 * 3600
    assert (autumn.end_utc - autumn.start_utc).total_seconds() == 25 * 3600


def test_rate_limiter_is_bounded_per_session_key():
    from live.dashboard_api import SessionRateLimiter
    limiter = SessionRateLimiter(limit=10, window_seconds=60)
    assert all(limiter.allow("session-a") for _ in range(10))
    assert limiter.allow("session-a") is False
    assert limiter.allow("session-b") is True


def test_query_deadline_fails_closed_with_stable_exception():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp)
        model = DashboardReadModel(LiveRepository(repo.db_path, query_only=True), query_timeout_seconds=0.001)
        try:
            model._one("""WITH RECURSIVE x(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM x WHERE n<10000000)
                          SELECT sum(n) AS value FROM x""")
            assert False, "expensive query should be interrupted"
        except DashboardQueryError as exc:
            assert "time budget" in str(exc)


def test_account_equity_current_query_uses_partial_index_without_temp_sort():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp)
        seed_verified_lifecycle(repo)
        sql = """
            SELECT * FROM live_account_snapshots
            WHERE environment=? AND execution_mode=?
              AND verification_status IN ('VERIFIED','RECONCILED')
              AND ingested_at>=?
            ORDER BY id DESC LIMIT 1
        """
        with repo.connect() as conn:
            plan = "\n".join(str(row[3]) for row in conn.execute(
                "EXPLAIN QUERY PLAN " + sql, ("LIVE", "REAL_TRADING", CUTOVER)
            ))
            selected = conn.execute(sql, ("LIVE", "REAL_TRADING", CUTOVER)).fetchone()
        assert selected is not None
        assert "idx_live_account_snapshots_dashboard_current" in plan
        assert "TEMP B-TREE" not in plan


def test_account_equity_does_not_fallback_to_sampled_at_after_cutover():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp)
        seed_verified_lifecycle(repo)
        with repo.connect() as conn:
            conn.execute("UPDATE live_account_snapshots SET ingested_at=NULL "
                         "WHERE environment='LIVE' AND execution_mode='REAL_TRADING'")
            conn.commit()
        equity = DashboardReadModel(LiveRepository(repo.db_path, query_only=True)).account_equity(now=NOW)
        assert equity["cash"]["value"] is None
        assert equity["cash"]["quality"] == "UNAVAILABLE"


def test_response_cache_single_flight_coalesces_concurrent_tabs():
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from live.dashboard_api import ResponseCache
    cache = ResponseCache(); calls = 0; lock = threading.Lock()
    def load():
        nonlocal calls
        with lock: calls += 1
        time.sleep(0.05)
        return {"quality": "REAL"}
    with ThreadPoolExecutor(max_workers=20) as pool:
        values = list(pool.map(lambda _index: cache.get("overview", 2.0, load), range(20)))
    assert calls == 1
    assert all(value == {"quality": "REAL"} for value in values)


def test_empty_positions_and_orders_require_post_cutover_reconciliation():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp)
        model = DashboardReadModel(LiveRepository(repo.db_path, query_only=True))
        assert model.open_positions(now=NOW)["quality"] == "UNAVAILABLE"
        assert model.open_orders()["quality"] == "UNAVAILABLE"
        equity = model.account_equity(now=NOW)
        assert equity["positions"]["value"] is None
        assert equity["positions"]["quality"] == "UNAVAILABLE"


def test_runtime_run_rotation_preserves_cutover_and_interrupts_previous_run():
    with tempfile.TemporaryDirectory() as tmp:
        repo, config = build_db(tmp)
        first = migrate_dashboard_schema(
            repo, config, run_id="runtime-one", rotate_runtime_run=True
        )
        second = migrate_dashboard_schema(
            repo, config, run_id="runtime-two", rotate_runtime_run=True
        )
        with repo.connect() as conn:
            rows = conn.execute(
                "SELECT run_id,state,ended_at FROM live_strategy_runs ORDER BY started_at,run_id"
            ).fetchall()
            cutover = conn.execute(
                "SELECT cutover_at,run_id FROM live_dashboard_cutovers WHERE environment=\x27LIVE\x27"
            ).fetchone()
        assert first.cutover_at == second.cutover_at == CUTOVER
        assert tuple(cutover) == (CUTOVER, "test-run")
        assert [(row["run_id"], row["state"]) for row in rows] == [
            ("runtime-one", "INTERRUPTED"), ("runtime-two", "RUNNING")
        ]
        assert rows[0]["ended_at"] is not None


def test_provenance_constraints_reject_invalid_insert_and_update():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp)
        with repo.connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO live_account_snapshots(sampled_at,status,environment) VALUES(?,?,?)",
                    (NOW.isoformat(), "ok", "INVALID"),
                )
                assert False, "invalid provenance insert should be rejected"
            except sqlite3.IntegrityError:
                pass
            conn.execute(
                "INSERT INTO live_account_snapshots(sampled_at,status) VALUES(?,?)",
                (NOW.isoformat(), "ok"),
            )
            row_id = conn.execute(
                "SELECT id FROM live_account_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            try:
                conn.execute(
                    "UPDATE live_account_snapshots SET verification_status=? WHERE id=?",
                    ("INVENTED", row_id),
                )
                assert False, "invalid provenance update should be rejected"
            except sqlite3.IntegrityError:
                pass


def test_position_and_redemption_events_receive_runtime_provenance():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _config = build_db(tmp)
        with repo.connect() as conn:
            conn.execute(
                """INSERT INTO live_strategy_positions(
                    position_id,event_id,condition_id,token_id,outcome,state,
                    acquired_shares_text,remaining_shares_text,sellable_shares_text,
                    average_entry_price_text,cost_all_in_text,resolved_winner,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "redeem-position", "redeem-event", "condition", "token", "YES",
                    "REDEEM_PENDING", "2", "2", "0", "0.5", "1", 1,
                    NOW.isoformat(), NOW.isoformat(),
                ),
            )
            event = conn.execute(
                "SELECT environment,execution_mode,run_id,position_id FROM live_position_events"
            ).fetchone()
            redemption = conn.execute(
                "SELECT environment,execution_mode,run_id,state FROM live_redemptions"
            ).fetchone()
        assert tuple(event) == ("LIVE", "REAL_TRADING", "test-run", "redeem-position")
        assert tuple(redemption) == ("LIVE", "REAL_TRADING", "test-run", "REDEEM_PENDING")


def test_session_nonce_and_explicit_statistics_and_freshness_endpoints():
    with tempfile.TemporaryDirectory() as tmp:
        repo, config = build_db(tmp)
        configure_dashboard_api(repo.db_path, config, trader_status_provider=lambda: None)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, base_url="https://testserver")
        from live.auth import LiveAuthManager

        auth = LiveAuthManager(config)
        first = auth.create_session(config.login_username)
        second = auth.create_session(config.login_username)
        assert first != second
        assert auth.verify_session(first) and auth.verify_session(second)
        client.cookies.set("live_session", first)
        statistics = client.get("/live/dashboard/v1/trade-statistics?range=today")
        freshness = client.get("/live/dashboard/v1/freshness")
        assert statistics.status_code == 200
        assert freshness.status_code == 200
        assert freshness.json()["data"]["stale"] is True
        assert "freshness_seconds" in statistics.json()["meta"]
        assert "stale" in statistics.json()["meta"]


def test_polymarket_fee_rate_is_converted_from_bps_to_currency_amount():
    from live.adapters.polymarket import RealPolymarketTradingAdapter

    normalized = RealPolymarketTradingAdapter._normalize_trade(
        {
            "id": "trade", "taker_order_id": "order", "market": "condition",
            "token_id": "token", "side": "BUY", "price": "0.5", "size": "10",
            "fee_rate_bps": "100", "status": "MATCHED",
        }
    )
    assert normalized["fee_rate_bps"] == "100"
    assert normalized["fee"] == "0.025"
    assert normalized["fee_verification_status"] == "VERIFIED"
    assert normalized["fee_source"] == "polymarket_fee_rate_bps"
