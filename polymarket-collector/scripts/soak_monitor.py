#!/usr/bin/env python3
"""
Independent, read-only 24h soak monitor for polymarket-btc-live.

HARD CONSTRAINTS (do not violate):
  - Never writes to trading application state or its SQLite DB.
  - Never calls any trader IPC command other than STATUS.
  - Opens SQLite strictly read-only (mode=ro, PRAGMA query_only=ON).
  - Never restarts / pauses / resumes / kill-switches the trader.
  - Only writes inside its own run directory.

Stdlib only. No third-party dependencies, so it does not need the
application virtualenv and cannot be broken by app dependency changes.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

FINAL_INTENT_STATES = {
    "FILLED", "PARTIAL_FINAL", "ZERO_FILL", "CANCELED",
    "REJECTED", "FAILED", "SETTLED", "REDEEMED",
}
ZERO_FILL_REASON_CODE = "REMOTE_MATCHED_ZERO_FILL"
ZERO_FILL_TRACK_SECONDS = 60.0
ZERO_FILL_MAX_TRACKED = 200
AUTO_RECOVERY_STABLE_MS = 30_000
STATE_KEYS = (
    "kill_switch", "pause_entries", "strategy_readiness", "strategy_block_reason",
    "reconciliation_readiness", "reconciliation_block_reason", "order_heartbeat_status",
    "market_ws_status", "user_ws_status", "last_successful_reconciliation_at",
    "pause_state", "pause_owner", "pause_cause", "release_policy", "pause_generation",
    "pause_acquired_at", "recovery_status", "recovery_engine_status",
    "recovery_blockers_json", "pause_eligible_since", "recovery_last_action",
    "recovery_last_result", "last_auto_recovery_at",
    "operator_action_required", "operator_action_reason",
    "global_entry_halt_required", "global_entry_halt_reason",
    "incident_scope", "quarantined_positions_count", "quarantine_last_at",
    "auto_repair_count_24h", "auto_repair_last_at",
    "orphaned_reconciliations_finalized_count",
    "orphaned_reconciliations_finalized_at",
)

_STOP = False


def _handle_signal(signum, _frame):
    global _STOP
    _STOP = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat().replace("+00:00", "Z")


class AppendWriter:
    """Append-only JSONL writer. Opens/flushes/closes per write -> nothing
    buffered in memory across the 24h run, resilient to the process being
    killed mid-write (no partial in-memory backlog to lose)."""

    def __init__(self, path: str):
        self.path = path

    def write(self, obj: dict) -> None:
        line = json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())


class Logger:
    def __init__(self, path: str):
        self.path = path

    def log(self, level: str, message: str) -> None:
        line = f"{utc_iso()} [{level}] {message}"
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
        print(line, flush=True)


# --------------------------------------------------------------------------
# Minimal, dependency-free IPC client speaking the same newline-delimited
# JSON protocol as live/ipc.py::TraderIPCClient. Kept standalone on purpose
# so this monitor has zero import dependency on application source.
# --------------------------------------------------------------------------

class ReadOnlyIPCClient:
    MAX_MESSAGE_BYTES = 1_048_576

    def __init__(self, socket_path: str, timeout_seconds: float = 3.0):
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    def call(self, command: str) -> dict:
        request = json.dumps(
            {"request_id": uuid.uuid4().hex, "command": command, "payload": {}},
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout_seconds)
            client.connect(self.socket_path)
            client.sendall(request)
            chunks = bytearray()
            while b"\n" not in chunks:
                part = client.recv(65536)
                if not part:
                    break
                chunks.extend(part)
                if len(chunks) > self.MAX_MESSAGE_BYTES:
                    raise RuntimeError("IPC response too large")
        raw = bytes(chunks).split(b"\n", 1)[0]
        response = json.loads(raw)
        if not response.get("ok"):
            error = response.get("error") or {}
            raise RuntimeError(f"{error.get('code')}: {error.get('message')}")
        return response.get("result") or {}


def get_trader_pid(trader_service: str) -> int | None:
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", trader_service],
            capture_output=True, text=True, timeout=5,
        )
        pid = int(out.stdout.strip())
        return pid if pid > 0 else None
    except Exception:
        return None


def read_proc_resources(pid: int) -> dict | None:
    """Read-only /proc inspection. No signals, no ptrace."""
    try:
        with open(f"/proc/{pid}/stat", "r") as fh:
            stat_fields = fh.read().split()
        with open(f"/proc/{pid}/status", "r") as fh:
            status_text = fh.read()
        clk_tck = os.sysconf("SC_CLK_TCK")
        utime = int(stat_fields[13])
        stime = int(stat_fields[14])
        num_threads = int(stat_fields[19])
        cpu_seconds = (utime + stime) / clk_tck
        vsz_kb = rss_kb = None
        for line in status_text.splitlines():
            if line.startswith("VmRSS:"):
                rss_kb = int(line.split()[1])
            elif line.startswith("VmSize:"):
                vsz_kb = int(line.split()[1])
        return {
            "pid": pid,
            "cpu_seconds_total": round(cpu_seconds, 3),
            "rss_kb": rss_kb,
            "vsz_kb": vsz_kb,
            "num_threads": num_threads,
        }
    except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
        return None


def read_self_resources() -> dict:
    data = read_proc_resources(os.getpid()) or {}
    data["pid"] = os.getpid()
    return data


def disk_free_bytes(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def sqlite_readonly_connect(db_path: str, timeout: float = 4.0) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def db_query_state(conn: sqlite3.Connection) -> dict:
    placeholders = ",".join("?" for _ in STATE_KEYS)
    rows = conn.execute(
        f"SELECT key, value FROM live_system_state WHERE key IN ({placeholders})",
        STATE_KEYS,
    ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def db_query_trading_snapshot(
    conn: sqlite3.Connection,
    last_watermark: str,
    propagation_watermark: str,
) -> dict:
    non_final_placeholders = ",".join("?" for _ in FINAL_INTENT_STATES)
    non_final_count = conn.execute(
        f"SELECT COUNT(*) AS c FROM live_strategy_intents "
        f"WHERE state NOT IN ({non_final_placeholders})",
        tuple(FINAL_INTENT_STATES),
    ).fetchone()["c"]

    active_positions = [dict(r) for r in conn.execute(
        "SELECT id, condition_id, token_id, outcome, size, status, created_at, updated_at "
        "FROM live_positions WHERE status='open' ORDER BY id DESC LIMIT 50"
    ).fetchall()]

    recon_run = conn.execute(
        "SELECT id, started_at, finished_at, status, gaps_count, gaps_json, error "
        "FROM live_reconciliation_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    recon_run = dict(recon_run) if recon_run else None
    recon_counts = dict(conn.execute(
        "SELECT COUNT(*) AS running_count, "
        "COALESCE(SUM(CASE WHEN julianday(started_at) < "
        "julianday('now','-5 minutes') THEN 1 ELSE 0 END),0) "
        "AS stuck_running_count FROM live_reconciliation_runs "
        "WHERE status='running'"
    ).fetchone())
    position_state_counts = {
        str(row["state"]): int(row["count"])
        for row in conn.execute(
            "SELECT state,COUNT(*) AS count FROM live_strategy_positions "
            "GROUP BY state"
        ).fetchall()
    }
    open_quarantines = [dict(row) for row in conn.execute(
        "SELECT quarantine_id,incident_scope,entity_type,entity_id,"
        "position_id,token_id,event_id,reason_code,"
        "operator_action_required,global_entry_halt_required,last_seen_at "
        "FROM live_quarantines WHERE status='OPEN' "
        "ORDER BY last_seen_at DESC LIMIT 100"
    ).fetchall()]
    active_alerts = [dict(row) for row in conn.execute(
        "SELECT id,severity,alert_type,reason_code,entity_type,entity_id,"
        "first_seen_at,last_seen_at,occurrence_count,recurrence_count,"
        "notification_status FROM live_alerts "
        "WHERE active=1 AND status='OPEN' ORDER BY last_seen_at DESC LIMIT 100"
    ).fetchall()]
    open_order_count = int(conn.execute(
        "SELECT COUNT(*) FROM live_orders WHERE status NOT IN "
        "('filled','cancelled','unmatched','failed')"
    ).fetchone()[0])

    latest_entry = conn.execute(
        "SELECT intent_id, token_id, condition_id, state, reason_code, created_at, final_at "
        "FROM live_strategy_intents WHERE action='ENTRY' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    latest_entry = dict(latest_entry) if latest_entry else None

    latest_exit = conn.execute(
        "SELECT intent_id, token_id, condition_id, state, reason_code, created_at, final_at "
        "FROM live_strategy_intents WHERE action='EXIT' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    latest_exit = dict(latest_exit) if latest_exit else None

    new_zero_fills = [dict(r) for r in conn.execute(
        "SELECT intent_id, token_id, condition_id, remote_order_id, "
        "requested_shares_text, requested_amount_text, final_at, created_at, position_id "
        "FROM live_strategy_intents WHERE reason_code=? AND final_at > ? "
        "ORDER BY final_at ASC LIMIT 50",
        (ZERO_FILL_REASON_CODE, last_watermark),
    ).fetchall()]

    new_propagation_intents = [dict(r) for r in conn.execute(
        "SELECT intent_id,token_id,condition_id,remote_order_id,action,state,"
        "reason_code,created_at,updated_at,final_at,position_id "
        "FROM live_strategy_intents WHERE reason_code=? AND updated_at > ? "
        "ORDER BY updated_at ASC LIMIT 50",
        ("REMOTE_MATCHED_FILL_PROPAGATION_PENDING", propagation_watermark),
    ).fetchall()]

    return {
        "non_final_intents_count": non_final_count,
        "active_positions": active_positions,
        "active_positions_count": len(active_positions),
        "latest_reconciliation_run": recon_run,
        "reconciliation_running": recon_counts,
        "strategy_position_state_counts": position_state_counts,
        "open_quarantines_count": len(open_quarantines),
        "open_quarantines": open_quarantines,
        "active_alerts_count": len(active_alerts),
        "active_alerts": active_alerts,
        "open_orders_count": open_order_count,
        "latest_entry_intent": latest_entry,
        "latest_exit_intent": latest_exit,
        "new_zero_fill_intents": new_zero_fills,
        "new_propagation_intents": new_propagation_intents,
    }


def db_lookup_token_state(conn: sqlite3.Connection, token_id: str, condition_id: str,
                           since: str) -> dict:
    """Read-only follow-up lookup used only while tracking a
    REMOTE_MATCHED_ZERO_FILL incident. Cheap, indexed/small-table scans."""
    positions = [dict(r) for r in conn.execute(
        "SELECT id, status, size, source, created_at, updated_at FROM live_positions "
        "WHERE token_id=? OR condition_id=? ORDER BY id DESC LIMIT 10",
        (token_id, condition_id),
    ).fetchall()]
    exits = [dict(r) for r in conn.execute(
        "SELECT intent_id, state, reason_code, created_at, final_at FROM live_strategy_intents "
        "WHERE action='EXIT' AND (token_id=? OR condition_id=?) AND created_at >= ? "
        "ORDER BY created_at DESC LIMIT 5",
        (token_id, condition_id, since),
    ).fetchall()]
    recent_gap_runs = [dict(r) for r in conn.execute(
        "SELECT id, finished_at, status, gaps_json FROM live_reconciliation_runs "
        "WHERE finished_at >= ? AND status != 'ok' ORDER BY id ASC LIMIT 20",
        (since,),
    ).fetchall()]
    return {"positions": positions, "exits": exits, "gap_runs": recent_gap_runs}


# --------------------------------------------------------------------------
# Market-ws telemetry extraction (matches live/market_websocket.py health())
# --------------------------------------------------------------------------

def extract_normalized(status: dict, trader_pid: int | None) -> dict:
    market_ws = status.get("market_ws") or {}
    user_ws = status.get("user_ws") or {}
    recovery = status.get("recovery") or {}
    pause = recovery.get("pause") or {}
    strategy = status.get("strategy") or {}
    telem = market_ws.get("hot_path_telemetry") or {}
    gen_timing = telem.get("generation_timing") or {}

    books = market_ws.get("books") or {}
    books_ready = sum(1 for b in books.values() if b.get("ready"))
    books_total = len(books)

    def q(prefix: str) -> dict:
        return {
            "current": telem.get(f"{prefix}_current"),
            "p50": telem.get(f"{prefix}_p50"),
            "p95": telem.get(f"{prefix}_p95"),
            "p99": telem.get(f"{prefix}_p99"),
            "high_watermark": telem.get(f"{prefix}_high_watermark"),
        }

    return {
        "pid": trader_pid,
        "market_ws": {
            "status": market_ws.get("status"),
            "stale": market_ws.get("stale"),
            "generation": gen_timing.get("generation"),
            "messages_received": market_ws.get("messages_received"),
            "reconnect_attempts": market_ws.get("reconnect_attempts"),
            "readiness_state": market_ws.get("readiness_state"),
            "best_price_mismatches": market_ws.get("best_price_mismatches"),
            "ingress_resyncs": market_ws.get("ingress_resyncs"),
            "ingress_queue_depth": market_ws.get("ingress_queue_depth"),
            "books_ready": books_ready,
            "books_total": books_total,
            "exchange_age_at_reader_ms": telem.get("exchange_age_at_reader_ms"),
            "ws_library_queue_depth": q("ws_library_queue_depth"),
            "event_loop_lag_ms": telem.get("event_loop_lag_ms"),
            "recv_to_recv_gap_ms": telem.get("recv_to_recv_gap_ms"),
            "market_processing_ms": telem.get("market_processing_ms"),
            "disconnects_total": telem.get("disconnects_total"),
            "reconnect_attempts_total": telem.get("reconnect_attempts_total"),
            "successful_connections_total": telem.get("successful_connections_total"),
            "connection_generations_total": telem.get("connection_generations_total"),
            "messages_total_by_reconnect_age_bucket": telem.get("messages_total_by_reconnect_age_bucket"),
            "stale_total_by_reconnect_age_bucket": telem.get("stale_total_by_reconnect_age_bucket"),
        },
        "user_ws": {
            "status": user_ws.get("status"),
            "stale": user_ws.get("stale"),
            "reconnect_attempts": user_ws.get("reconnect_attempts"),
            "reconnect_count": user_ws.get("reconnect_count"),
            "event_queue_depth": user_ws.get("event_queue_depth"),
            "event_queue_dropped": user_ws.get("event_queue_dropped"),
        },
        "reconciliation": status.get("reconciliation") or {},
        "strategy": {
            "enabled": strategy.get("enabled"),
            "mode": strategy.get("mode"),
            "readiness": strategy.get("readiness"),
            "block_reason": strategy.get("block_reason"),
            "last_error": strategy.get("last_error"),
            "positions_count": len(strategy.get("positions") or []),
            "active_alerts": strategy.get("active_alerts"),
            "critical_email_pending": strategy.get("critical_email_pending"),
            "frame_queue_depth": strategy.get("frame_queue_depth"),
        },
        "pause": {
            "pause_entries": pause.get("pause_entries"),
            "pause_state": pause.get("pause_state"),
            "pause_owner": pause.get("pause_owner"),
            "pause_cause": pause.get("pause_cause") or pause.get("pause_reason"),
            "release_policy": pause.get("release_policy"),
            "pause_generation": pause.get("pause_generation"),
            "operator_action_required": pause.get("operator_action_required"),
            "operator_action_reason": pause.get("operator_action_reason"),
            "global_entry_halt_required": pause.get("global_entry_halt_required"),
            "global_entry_halt_reason": pause.get("global_entry_halt_reason"),
            "incident_scope": pause.get("incident_scope"),
        },
        "recovery": {
            "trading_status": recovery.get("trading_status"),
            "auto_recovery_status": recovery.get("auto_recovery_status"),
            "last_recovery_action": recovery.get("last_recovery_action"),
            "last_recovery_result": recovery.get("last_recovery_result"),
            "current_blockers": recovery.get("current_blockers"),
            "stability_elapsed_ms": recovery.get("stability_elapsed_ms"),
            "stability_target_ms": recovery.get("stability_target_ms"),
        },
    }


# --------------------------------------------------------------------------
# Hourly rolling aggregation (bounded memory: counters only, reset hourly)
# --------------------------------------------------------------------------

class HourlyAggregator:
    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        self._reset()
        if not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0:
            with open(csv_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "hour_start_utc,hour_end_utc,sample_count,status_read_failures,"
                    "market_ws_disconnects,pause_acquired_count,pause_released_count,"
                    "reconciliation_gap_count,reconciliation_failed_count,"
                    "reconciliation_contradiction_count,zero_fill_events,"
                    "avg_exchange_age_p95_ms,max_event_loop_lag_ms\n"
                )

    def _reset(self):
        self.hour_start = utc_now()
        self.sample_count = 0
        self.status_failures = 0
        self.disconnects = 0
        self.pause_acquired = 0
        self.pause_released = 0
        self.recon_gap = 0
        self.recon_failed = 0
        self.recon_contradiction = 0
        self.zero_fill = 0
        self._age_p95_samples: list[float] = []
        self._max_loop_lag = 0.0

    def observe_sample(self, normalized: dict | None, had_error: bool):
        self.sample_count += 1
        if had_error:
            self.status_failures += 1
            return
        mw = normalized.get("market_ws", {})
        age = (mw.get("exchange_age_at_reader_ms") or {}).get("p95")
        if isinstance(age, (int, float)):
            self._age_p95_samples.append(age)
        loop_lag = mw.get("event_loop_lag_ms") or {}
        max_val = loop_lag.get("max")
        if isinstance(max_val, (int, float)):
            self._max_loop_lag = max(self._max_loop_lag, max_val)

    def observe_event(self, event_type: str):
        if event_type == "MARKET_WS_DISCONNECTED":
            self.disconnects += 1
        elif event_type == "PAUSE_ACQUIRED":
            self.pause_acquired += 1
        elif event_type == "PAUSE_RELEASED":
            self.pause_released += 1
        elif event_type == "RECONCILIATION_GAP":
            self.recon_gap += 1
        elif event_type == "RECONCILIATION_FAILED":
            self.recon_failed += 1
        elif event_type == "RECONCILIATION_CONTRADICTION":
            self.recon_contradiction += 1
        elif event_type == "REMOTE_MATCHED_ZERO_FILL":
            self.zero_fill += 1

    def maybe_flush(self, force: bool = False):
        elapsed = (utc_now() - self.hour_start).total_seconds()
        if not force and elapsed < 3600:
            return
        avg_age_p95 = round(statistics.mean(self._age_p95_samples), 3) if self._age_p95_samples else ""
        row = [
            utc_iso(self.hour_start), utc_iso(), str(self.sample_count),
            str(self.status_failures), str(self.disconnects), str(self.pause_acquired),
            str(self.pause_released), str(self.recon_gap), str(self.recon_failed),
            str(self.recon_contradiction), str(self.zero_fill),
            str(avg_age_p95), str(round(self._max_loop_lag, 3)),
        ]
        with open(self.csv_path, "a", encoding="utf-8") as fh:
            fh.write(",".join(row) + "\n")
            fh.flush()
        self._reset()


# --------------------------------------------------------------------------
# Main monitor loop
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only 24h soak monitor")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--duration-seconds", type=float, default=86400.0)
    parser.add_argument("--sample-interval", type=float, default=10.0)
    parser.add_argument("--resource-interval", type=float, default=60.0)
    parser.add_argument("--db-interval", type=float, default=45.0)
    parser.add_argument("--db-path", default="/opt/polymarket-btc-live/poly_live.sqlite3")
    parser.add_argument("--socket-path", default="/run/polymarket/trader.sock")
    parser.add_argument("--trader-service", default="polymarket-trader.service")
    args = parser.parse_args()

    run_dir = args.run_dir
    os.makedirs(os.path.join(run_dir, "incidents"), exist_ok=True)
    os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)
    # Ensure the full expected artifact layout exists from the start, even
    # before the first event/incident is ever written.
    for fname in ("samples.jsonl", "events.jsonl", "resources.jsonl"):
        fpath = os.path.join(run_dir, fname)
        if not os.path.exists(fpath):
            open(fpath, "a").close()

    samples_w = AppendWriter(os.path.join(run_dir, "samples.jsonl"))
    events_w = AppendWriter(os.path.join(run_dir, "events.jsonl"))
    resources_w = AppendWriter(os.path.join(run_dir, "resources.jsonl"))
    logger = Logger(os.path.join(run_dir, "logs", "monitor.log"))
    metadata_path = os.path.join(run_dir, "metadata.json")

    start_dt = utc_now()
    start_monotonic = time.monotonic()
    end_target_dt = start_dt.timestamp() + args.duration_seconds

    ipc = ReadOnlyIPCClient(args.socket_path, timeout_seconds=5.0)
    aggregator = HourlyAggregator(os.path.join(run_dir, "hourly_summary.csv"))

    trader_pid = get_trader_pid(args.trader_service)

    # Write initial metadata (updated again at completion).
    metadata = {
        "start_utc": utc_iso(start_dt),
        "expected_end_utc": utc_iso(datetime.fromtimestamp(end_target_dt, timezone.utc)),
        "monitor_pid": os.getpid(),
        "monitor_service": "polymarket-soak-24h.service",
        "trader_pid_at_start": trader_pid,
        "trader_start_timestamp": None,
        "git_head": None,
        "git_status_summary": None,
        "websockets_version": None,
        "sample_interval_seconds": args.sample_interval,
        "resource_interval_seconds": args.resource_interval,
        "db_interval_seconds": args.db_interval,
        "artifact_directory": run_dir,
        "completed": False,
    }
    try:
        show = subprocess.run(
            ["systemctl", "show", "-p", "ExecMainStartTimestamp", "--value", args.trader_service],
            capture_output=True, text=True, timeout=5,
        )
        metadata["trader_start_timestamp"] = show.stdout.strip() or None
    except Exception:
        pass
    try:
        gh = subprocess.run(
            ["git", "-C", "/opt/polymarket-btc-live/repo", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        metadata["git_head"] = gh.stdout.strip() or None
        gs = subprocess.run(
            ["git", "-C", "/opt/polymarket-btc-live/repo", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        metadata["git_status_summary"] = f"{len(gs.stdout.splitlines())} changed file(s)"
    except Exception:
        pass
    try:
        wv = subprocess.run(
            ["/opt/polymarket-btc-live/.venv/bin/python3", "-c",
             "import websockets; print(websockets.__version__)"],
            capture_output=True, text=True, timeout=10,
        )
        metadata["websockets_version"] = wv.stdout.strip() or None
    except Exception:
        pass

    with open(metadata_path, "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, sort_keys=True)

    logger.log("INFO", f"soak monitor starting: run_dir={run_dir} pid={os.getpid()} "
                        f"duration={args.duration_seconds}s trader_pid={trader_pid}")

    prev_normalized: dict | None = None
    prev_recon_run_id: int | None = None
    zero_fill_watermark = utc_iso(start_dt)
    propagation_watermark = utc_iso(start_dt)
    zero_fill_tracking: dict[str, dict] = {}
    flagged_pause_generations: set = set()
    consecutive_status_failures = 0
    reconnect_times: deque[float] = deque()
    last_ws_storm_emit = float("-inf")
    prev_db_state: dict | None = None
    prev_quarantine_ids: set[str] = set()
    stuck_reconciliation_active = False

    last_sample_t = 0.0
    last_resource_t = 0.0
    last_db_t = 0.0

    while True:
        now_m = time.monotonic()
        elapsed = now_m - start_monotonic

        if _STOP:
            logger.log("INFO", "SIGTERM/SIGINT received; shutting down after final sample")
            break
        if elapsed >= args.duration_seconds:
            logger.log("INFO", "target duration reached")
            break

        did_something = False

        # ---- STATUS sample (every sample-interval) ----
        if now_m - last_sample_t >= args.sample_interval:
            last_sample_t = now_m
            did_something = True
            ts = utc_iso()
            cur_pid = get_trader_pid(args.trader_service)
            sample_record = {"timestamp": ts, "pid": cur_pid}
            had_error = False
            try:
                status = ipc.call("STATUS")
                normalized = extract_normalized(status, cur_pid)
                sample_record["normalized"] = normalized
                sample_record["status_raw"] = status
                consecutive_status_failures = 0
            except Exception as exc:  # resilient to a single STATUS read failure
                had_error = True
                normalized = None
                consecutive_status_failures += 1
                sample_record["error"] = f"{type(exc).__name__}: {exc}"[:500]
                logger.log("WARN", f"STATUS read failed ({consecutive_status_failures}x): {exc}")

            samples_w.write(sample_record)
            aggregator.observe_sample(normalized or {}, had_error)

            if not had_error:
                # ---- event detection vs previous good sample ----
                if prev_normalized is not None:
                    pn, cn = prev_normalized, normalized
                    if pn.get("pid") is not None and cn.get("pid") is not None and pn["pid"] != cn["pid"]:
                        ev = {"type": "SERVICE_PID_CHANGED", "timestamp": ts,
                              "old_pid": pn["pid"], "new_pid": cn["pid"]}
                        events_w.write(ev)
                        logger.log("EVENT", str(ev))

                    pmw, cmw = pn.get("market_ws", {}), cn.get("market_ws", {})
                    if pmw.get("status") != cmw.get("status"):
                        if cmw.get("status") not in ("CONNECTED",) and pmw.get("status") == "CONNECTED":
                            evtype = "MARKET_WS_DISCONNECTED"
                        elif cmw.get("status") == "CONNECTED" and pmw.get("status") != "CONNECTED":
                            evtype = "MARKET_WS_CONNECTED"
                        else:
                            evtype = "MARKET_WS_DISCONNECTED" if cmw.get("status") != "CONNECTED" else "MARKET_WS_CONNECTED"
                        ev = {"type": evtype, "timestamp": ts,
                              "old_status": pmw.get("status"), "new_status": cmw.get("status")}
                        events_w.write(ev)
                        aggregator.observe_event(evtype)
                        logger.log("EVENT", str(ev))

                    if pmw.get("generation") != cmw.get("generation") and cmw.get("generation") is not None:
                        ev = {"type": "GENERATION_CHANGED", "timestamp": ts,
                              "old_generation": pmw.get("generation"), "new_generation": cmw.get("generation")}
                        events_w.write(ev)
                        logger.log("EVENT", str(ev))

                    # A reconnect storm is three or more new reconnect attempts
                    # inside a rolling 60-second window.  Emit at most once per
                    # window so the evidence remains useful rather than noisy.
                    old_reconnects = pmw.get("reconnect_attempts_total")
                    new_reconnects = cmw.get("reconnect_attempts_total")
                    if isinstance(old_reconnects, (int, float)) and isinstance(new_reconnects, (int, float)):
                        reconnect_delta = max(0, min(100, int(new_reconnects - old_reconnects)))
                        reconnect_times.extend([now_m] * reconnect_delta)
                    while reconnect_times and now_m - reconnect_times[0] > 60.0:
                        reconnect_times.popleft()
                    if len(reconnect_times) >= 3 and now_m - last_ws_storm_emit >= 60.0:
                        last_ws_storm_emit = now_m
                        ev = {"type": "WS_STORM", "timestamp": ts,
                              "reconnect_attempts_60s": len(reconnect_times),
                              "market_ws": cmw}
                        events_w.write(ev)
                        logger.log("EVENT", str(ev))

                    if pmw.get("best_price_mismatches") != cmw.get("best_price_mismatches"):
                        ev = {"type": "BEST_PRICE_MISMATCH_CHANGE", "timestamp": ts,
                              "old_value": pmw.get("best_price_mismatches"),
                              "new_value": cmw.get("best_price_mismatches")}
                        events_w.write(ev)
                        logger.log("EVENT", str(ev))

                    pp, cp = pn.get("pause", {}), cn.get("pause", {})
                    if not pp.get("pause_entries") and cp.get("pause_entries"):
                        ev = {"type": "PAUSE_ACQUIRED", "timestamp": ts, "pause": cp}
                        events_w.write(ev)
                        aggregator.observe_event("PAUSE_ACQUIRED")
                        logger.log("EVENT", str(ev))
                    elif pp.get("pause_entries") and not cp.get("pause_entries"):
                        ev = {"type": "PAUSE_RELEASED", "timestamp": ts,
                              "previous_pause": pp, "current_pause": cp}
                        events_w.write(ev)
                        aggregator.observe_event("PAUSE_RELEASED")
                        logger.log("EVENT", str(ev))

                    # AUTO_RECOVERY_STUCK: still paused, policy is
                    # AUTO_WHEN_CLEAN, gates clean >= 30s, not yet released.
                    cr = cn.get("recovery", {})
                    if (cp.get("pause_entries") and cp.get("release_policy") == "AUTO_WHEN_CLEAN"
                            and not cr.get("current_blockers")
                            and isinstance(cr.get("stability_elapsed_ms"), (int, float))
                            and cr["stability_elapsed_ms"] >= AUTO_RECOVERY_STABLE_MS):
                        gen = cp.get("pause_generation")
                        if gen not in flagged_pause_generations:
                            flagged_pause_generations.add(gen)
                            ev = {"type": "AUTO_RECOVERY_STUCK", "timestamp": ts,
                                  "pause_generation": gen, "pause": cp, "recovery": cr}
                            events_w.write(ev)
                            logger.log("EVENT", str(ev))
                    if not cp.get("pause_entries"):
                        flagged_pause_generations.discard(cp.get("pause_generation"))

                prev_normalized = normalized
            # keep prev_normalized on error too? No: leave last good sample so
            # a single failed read doesn't spuriously trigger transition
            # events on the next good read.

        # ---- resource sample (every resource-interval) ----
        if now_m - last_resource_t >= args.resource_interval:
            last_resource_t = now_m
            did_something = True
            ts = utc_iso()
            cur_pid = get_trader_pid(args.trader_service)
            trader_res = read_proc_resources(cur_pid) if cur_pid else None
            self_res = read_self_resources()
            try:
                load1, load5, load15 = os.getloadavg()
            except OSError:
                load1 = load5 = load15 = None
            try:
                mem_total_kb = mem_avail_kb = None
                with open("/proc/meminfo") as fh:
                    for line in fh:
                        if line.startswith("MemAvailable:"):
                            mem_avail_kb = int(line.split()[1])
                        elif line.startswith("MemTotal:"):
                            mem_total_kb = int(line.split()[1])
            except FileNotFoundError:
                mem_total_kb = mem_avail_kb = None

            db_size = wal_size = shm_size = None
            try:
                db_size = os.path.getsize(args.db_path)
                if os.path.exists(args.db_path + "-wal"):
                    wal_size = os.path.getsize(args.db_path + "-wal")
                if os.path.exists(args.db_path + "-shm"):
                    shm_size = os.path.getsize(args.db_path + "-shm")
            except OSError:
                pass
            try:
                free_bytes = disk_free_bytes(os.path.dirname(args.db_path))
            except OSError:
                free_bytes = None

            rec = {
                "timestamp": ts,
                "trader_pid": cur_pid,
                "trader": trader_res,
                "monitor_self": self_res,
                "load_average": {"1m": load1, "5m": load5, "15m": load15},
                "memory_kb": {"total": mem_total_kb, "available": mem_avail_kb},
                "sqlite": {"db_bytes": db_size, "wal_bytes": wal_size, "shm_bytes": shm_size},
                "disk_free_bytes": free_bytes,
            }
            resources_w.write(rec)

        # ---- DB sample (every db-interval), read-only ----
        if now_m - last_db_t >= args.db_interval:
            last_db_t = now_m
            did_something = True
            ts = utc_iso()
            try:
                conn = sqlite_readonly_connect(args.db_path)
                try:
                    state = db_query_state(conn)
                    snap = db_query_trading_snapshot(
                        conn, zero_fill_watermark, propagation_watermark
                    )

                    db_record = {"timestamp": ts, "state": state, "snapshot": {
                        k: v for k, v in snap.items()
                        if k not in ("new_zero_fill_intents", "new_propagation_intents")
                    }}
                    samples_w.write({"timestamp": ts, "db_sample": db_record})

                    # Operational state transitions required by the soak
                    # evidence contract.  The first DB poll is only a baseline.
                    current_quarantine_ids = {
                        str(q.get("quarantine_id"))
                        for q in (snap.get("open_quarantines") or [])
                    }
                    if prev_db_state is not None:
                        if (prev_db_state.get("global_entry_halt_required") != "true"
                                and state.get("global_entry_halt_required") == "true"):
                            ev = {"type": "GLOBAL_HALT", "timestamp": ts,
                                  "reason": state.get("global_entry_halt_reason"),
                                  "incident_scope": state.get("incident_scope")}
                            events_w.write(ev)
                            logger.log("EVENT", str(ev))

                        unknown_now = (
                            state.get("operator_action_required") == "true"
                            and "UNKNOWN" in str(
                                state.get("operator_action_reason")
                                or state.get("pause_cause") or ""
                            ).upper()
                        )
                        unknown_before = (
                            prev_db_state.get("operator_action_required") == "true"
                            and "UNKNOWN" in str(
                                prev_db_state.get("operator_action_reason")
                                or prev_db_state.get("pause_cause") or ""
                            ).upper()
                        )
                        if unknown_now and not unknown_before:
                            ev = {"type": "UNKNOWN_CAUSE", "timestamp": ts,
                                  "pause_cause": state.get("pause_cause"),
                                  "operator_action_reason": state.get("operator_action_reason")}
                            events_w.write(ev)
                            logger.log("EVENT", str(ev))

                        old_repair = prev_db_state.get("auto_repair_last_at") or ""
                        new_repair = state.get("auto_repair_last_at") or ""
                        if new_repair and new_repair != old_repair:
                            ev = {"type": "AUTO_REPAIR", "timestamp": ts,
                                  "auto_repair_last_at": new_repair,
                                  "auto_repair_count_24h": state.get("auto_repair_count_24h")}
                            events_w.write(ev)
                            logger.log("EVENT", str(ev))

                        for quarantine in (snap.get("open_quarantines") or []):
                            if str(quarantine.get("quarantine_id")) not in prev_quarantine_ids:
                                ev = {"type": "QUARANTINE", "timestamp": ts,
                                      "quarantine": quarantine}
                                events_w.write(ev)
                                logger.log("EVENT", str(ev))

                    running = snap.get("reconciliation_running") or {}
                    stuck_count = int(running.get("stuck_running_count") or 0)
                    if stuck_count > 0 and not stuck_reconciliation_active:
                        stuck_reconciliation_active = True
                        ev = {"type": "STUCK_RECONCILIATION", "timestamp": ts,
                              "running_count": int(running.get("running_count") or 0),
                              "stuck_running_count": stuck_count}
                        events_w.write(ev)
                        logger.log("EVENT", str(ev))
                    elif stuck_count == 0:
                        stuck_reconciliation_active = False

                    prev_db_state = dict(state)
                    prev_quarantine_ids = current_quarantine_ids

                    # reconciliation run transition (only evaluate completed
                    # runs; a run row is inserted with status='running' and
                    # finished_at=NULL before it completes)
                    run = snap.get("latest_reconciliation_run")
                    if run and run.get("finished_at") and run["id"] != prev_recon_run_id:
                        prev_recon_run_id = run["id"]
                        if run["status"] not in ("ok",):
                            block_reason = state.get("reconciliation_block_reason") or ""
                            if run["status"] == "gaps" and block_reason == "RECONCILIATION_CONTRADICTION":
                                evtype = "RECONCILIATION_CONTRADICTION"
                            elif run["status"] == "gaps":
                                evtype = "RECONCILIATION_GAP"
                            else:
                                evtype = "RECONCILIATION_FAILED"
                            ev = {"type": evtype, "timestamp": ts, "run": run,
                                  "block_reason": block_reason}
                            events_w.write(ev)
                            aggregator.observe_event(evtype)
                            logger.log("EVENT", str(ev))

                            if evtype == "RECONCILIATION_CONTRADICTION":
                                gaps = []
                                try:
                                    gaps = json.loads(run.get("gaps_json") or "[]")
                                except Exception:
                                    pass
                                incident = {
                                    "detected_at": ts,
                                    "reconciliation_run": run,
                                    "gaps_json": gaps,
                                    "affected": [
                                        {"token_id": g.get("token_id"),
                                         "condition_id": g.get("condition_id")}
                                        for g in gaps if isinstance(g, dict)
                                    ],
                                    "known_remote_propagation_pattern": "UNKNOWN",
                                }
                                # Heuristic per spec section 9: recent ENTRY +
                                # REMOTE_MATCHED_ZERO_FILL + a
                                # remote_position_corrected_local gap for the
                                # same token, discovered within ~15s.
                                gap_types = {g.get("type") for g in gaps if isinstance(g, dict)}
                                if "remote_position_corrected_local" in gap_types:
                                    match = False
                                    for g in gaps:
                                        if g.get("type") != "remote_position_corrected_local":
                                            continue
                                        tok = g.get("token_id")
                                        for tracked in zero_fill_tracking.values():
                                            if tracked["token_id"] == tok:
                                                match = True
                                    incident["known_remote_propagation_pattern"] = (
                                        "YES" if match else "NO"
                                    )
                                else:
                                    incident["known_remote_propagation_pattern"] = "NO"
                                fname = os.path.join(
                                    run_dir, "incidents",
                                    f"contradiction_{run['id']}_{ts.replace(':', '')}.json",
                                )
                                with open(fname, "w", encoding="utf-8") as fh:
                                    json.dump(incident, fh, indent=2, sort_keys=True, default=str)
                                logger.log("INCIDENT", f"RECONCILIATION_CONTRADICTION -> {fname}")

                    # ENTRY / EXIT / POSITION transitions (baseline on first
                    # DB poll only -- do not treat pre-existing rows as new)
                    entry = snap.get("latest_entry_intent")
                    if entry:
                        prior = getattr(main, "_last_entry_id", "__unset__")
                        main._last_entry_id = entry.get("intent_id")
                        if prior != "__unset__" and entry.get("intent_id") != prior:
                            ev = {"type": "ENTRY_CREATED", "timestamp": ts, "intent": entry}
                            events_w.write(ev)
                            logger.log("EVENT", str(ev))
                    exit_i = snap.get("latest_exit_intent")
                    if exit_i:
                        prior = getattr(main, "_last_exit_id", "__unset__")
                        main._last_exit_id = exit_i.get("intent_id")
                        if prior != "__unset__" and exit_i.get("intent_id") != prior:
                            ev = {"type": "EXIT_CREATED", "timestamp": ts, "intent": exit_i}
                            events_w.write(ev)
                            logger.log("EVENT", str(ev))
                    positions = snap.get("active_positions") or []
                    if positions:
                        max_id = max(p["id"] for p in positions)
                        if max_id != getattr(main, "_last_position_id", None):
                            if getattr(main, "_last_position_id", None) is not None:
                                ev = {"type": "POSITION_CREATED", "timestamp": ts,
                                      "position": next(p for p in positions if p["id"] == max_id)}
                                events_w.write(ev)
                                logger.log("EVENT", str(ev))
                            main._last_position_id = max_id

                    # A remotely matched order whose fill is still
                    # propagating must remain visible as its own evidence type.
                    for intent in (snap.get("new_propagation_intents") or []):
                        updated_at = intent.get("updated_at") or intent.get("created_at") or ts
                        if updated_at and updated_at > propagation_watermark:
                            propagation_watermark = updated_at
                        ev = {"type": "REMOTE_MATCHED_EXIT_PROPAGATION",
                              "timestamp": ts, "intent": intent}
                        events_w.write(ev)
                        logger.log("INCIDENT",
                                   f"REMOTE_MATCHED_EXIT_PROPAGATION intent={intent.get('intent_id')}")

                    # New REMOTE_MATCHED_ZERO_FILL intents -> start incident tracking
                    new_zf = snap.get("new_zero_fill_intents") or []
                    for intent in new_zf:
                        final_at = intent.get("final_at") or intent.get("created_at") or ts
                        if final_at and final_at > zero_fill_watermark:
                            zero_fill_watermark = final_at
                        ev = {"type": "REMOTE_MATCHED_ZERO_FILL", "timestamp": ts, "intent": intent}
                        events_w.write(ev)
                        aggregator.observe_event("REMOTE_MATCHED_ZERO_FILL")
                        logger.log("INCIDENT", f"REMOTE_MATCHED_ZERO_FILL intent={intent.get('intent_id')}")
                        if len(zero_fill_tracking) < ZERO_FILL_MAX_TRACKED:
                            zero_fill_tracking[intent["intent_id"]] = {
                                "intent_id": intent["intent_id"],
                                "token_id": intent.get("token_id"),
                                "condition_id": intent.get("condition_id"),
                                "remote_order_id": intent.get("remote_order_id"),
                                "requested_amount": (intent.get("requested_shares_text")
                                                      or intent.get("requested_amount_text")),
                                "final_at": final_at,
                                "start_monotonic": now_m,
                                "started_ts": ts,
                                "resolved": False,
                            }

                    # Advance / finalize tracked zero-fill incidents
                    finalize_ids = []
                    for iid, tracked in zero_fill_tracking.items():
                        if now_m - tracked["start_monotonic"] < ZERO_FILL_TRACK_SECONDS:
                            continue
                        follow = db_lookup_token_state(
                            conn, tracked["token_id"], tracked["condition_id"],
                            tracked["started_ts"],
                        )
                        position_found = bool(follow["positions"])
                        time_to_discovery = None
                        shares_discovered = None
                        if position_found:
                            first_pos = follow["positions"][-1]
                            time_to_discovery = (
                                _iso_to_epoch(first_pos.get("created_at")) -
                                _iso_to_epoch(tracked["started_ts"])
                                if first_pos.get("created_at") else None
                            )
                            shares_discovered = first_pos.get("size")
                        corrected_local = any(
                            isinstance(g, dict) and g.get("type") == "remote_position_corrected_local"
                            and g.get("token_id") == tracked["token_id"]
                            for run_row in follow["gap_runs"]
                            for g in (json.loads(run_row.get("gaps_json") or "[]")
                                      if run_row.get("gaps_json") else [])
                        )
                        contradiction_seen = any(
                            run_row.get("status") == "gaps" for run_row in follow["gap_runs"]
                        )
                        pause_state = db_query_state(conn)
                        exit_created = bool(follow["exits"])
                        final_state = follow["positions"][-1]["status"] if follow["positions"] else "NO_POSITION_FOUND"

                        incident = {
                            "intent_id": tracked["intent_id"],
                            "token_id": tracked["token_id"],
                            "condition_id": tracked["condition_id"],
                            "remote_order_id": tracked["remote_order_id"],
                            "requested_amount": tracked["requested_amount"],
                            "final_at": tracked["final_at"],
                            "tracked_started_at": tracked["started_ts"],
                            "tracked_resolved_at": ts,
                            "position_appeared": position_found,
                            "time_to_position_discovery_seconds": time_to_discovery,
                            "shares_discovered": shares_discovered,
                            "remote_position_corrected_local_seen": corrected_local,
                            "reconciliation_contradiction_seen": contradiction_seen,
                            "pause_entries_became_true": pause_state.get("pause_entries") == "true",
                            "exit_created": exit_created,
                            "final_position_state": final_state,
                        }
                        fname = os.path.join(
                            run_dir, "incidents",
                            f"zero_fill_{tracked['intent_id']}.json",
                        )
                        with open(fname, "w", encoding="utf-8") as fh:
                            json.dump(incident, fh, indent=2, sort_keys=True, default=str)
                        logger.log("INCIDENT", f"zero-fill incident resolved -> {fname}")
                        finalize_ids.append(iid)
                    for iid in finalize_ids:
                        zero_fill_tracking.pop(iid, None)

                finally:
                    conn.close()
            except sqlite3.Error as exc:
                logger.log("WARN", f"DB read failed (resilient, will retry): {exc}")

        aggregator.maybe_flush()

        if not did_something:
            time.sleep(1.0)

    # ---- Final sample + metadata ----
    logger.log("INFO", "performing final sample before exit")
    try:
        cur_pid = get_trader_pid(args.trader_service)
        status = ipc.call("STATUS")
        normalized = extract_normalized(status, cur_pid)
        samples_w.write({"timestamp": utc_iso(), "pid": cur_pid,
                          "normalized": normalized, "status_raw": status, "final_sample": True})
    except Exception as exc:
        samples_w.write({"timestamp": utc_iso(), "final_sample": True,
                          "error": f"{type(exc).__name__}: {exc}"[:500]})

    aggregator.maybe_flush(force=True)

    end_dt = utc_now()
    metadata["completed"] = not _STOP or (time.monotonic() - start_monotonic) >= args.duration_seconds
    metadata["end_utc"] = utc_iso(end_dt)
    metadata["duration_seconds"] = round(time.monotonic() - start_monotonic, 3)
    with open(metadata_path, "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, sort_keys=True)

    logger.log("INFO", f"soak monitor exiting cleanly. completed={metadata['completed']} "
                        f"duration={metadata['duration_seconds']}s")
    return 0


def _iso_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


if __name__ == "__main__":
    sys.exit(main())
