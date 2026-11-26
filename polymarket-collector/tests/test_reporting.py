from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository
from live.reporting import EmailService, ReportingService, SMTPSettings

UTC = timezone.utc

def setup_db(path):
    repo = LiveRepository(path); repo.migrate(False)
    StrategyRepository(repo).migrate(pause_entries_default=False)
    for key, value in {"kill_switch":"false", "pause_entries":"false", "strategy_readiness":"READY", "reconciliation_readiness":"READY", "market_ws_status":"CONNECTED", "user_ws_status":"CONNECTED"}.items():
        repo.set_state(key, value, "operator" if key == "kill_switch" else "test")
    return repo

def add_event(repo, start, number, trade=False):
    event_id=f"btc-updown-5m-{int(start.timestamp())}"; now=datetime.now(UTC).isoformat()
    with repo.connect() as conn:
        conn.execute("INSERT INTO live_markets(event_id,condition_id,last_update_at,raw_market_info,created_at,updated_at) VALUES(?,?,?,?,?,?)", (event_id,f"condition-{number}",now,json.dumps({"slug":event_id}),now,now))
        if trade:
            conn.execute("INSERT INTO live_strategy_deals(deal_id,event_id,state,outcome,trigger_price_text,total_fees_text,realized_pnl_text,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (f"deal-{number}",event_id,"CLOSED","YES","0.74","0","1",now,now))
            conn.execute("INSERT INTO live_strategy_intents(intent_id,correlation_id,event_id,condition_id,action,purpose,side,state,requested_amount_text,requested_shares_text,price_limit_text,filled_shares_text,remaining_shares_text,fee_text,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"intent-{number}",f"corr-{number}",event_id,f"condition-{number}","ENTRY","ENTRY","BUY","FILLED","5","6.7","0.74","6.7","0","0",now,now))

def test_synthetic_hour_duplicate_and_exactly_once(tmp_path):
    path=tmp_path/"db.sqlite"; repo=setup_db(path); start=datetime(2026,8,20,0,tzinfo=UTC)
    for i in range(12): add_event(repo,start+timedelta(minutes=5*i),i,trade=i<3)
    sent=[]; service=ReportingService(path,EmailService(SMTPSettings(sender="bot@example.com",recipients=("ops@example.com",)),sent.append)); service.migrate(); service.finalize_events(start+timedelta(hours=2))
    first=service.generate_hour(start,start+timedelta(hours=1)); second=service.generate_hour(start,start+timedelta(hours=1))
    assert (first["event_count"],first["trade_count"],first["no_trade_count"])==(12,3,9)
    assert second["email_status"]=="SENT" and second["email_attempts"]==1 and len(sent)==1
    with sqlite3.connect(path) as conn: assert conn.execute("SELECT count(*) FROM live_hourly_reports").fetchone()[0]==1

def test_inactive_hour_sends_nothing(tmp_path):
    path=tmp_path/"db.sqlite"; setup_db(path); sent=[]
    service=ReportingService(path,EmailService(SMTPSettings(sender="x",recipients=("y",)),sent.append)); service.migrate()
    assert service.generate_hour(datetime(2026,8,20,12,tzinfo=UTC),datetime(2026,8,20,13,tzinfo=UTC)) is None and sent==[]

def test_partial_coverage_one_email(tmp_path,monkeypatch):
    path=tmp_path/"db.sqlite"; setup_db(path); sent=[]
    service=ReportingService(path,EmailService(SMTPSettings(sender="x",recipients=("y",)),sent.append)); service.migrate()
    monkeypatch.setattr("live.reporting.LiveStrategyRuntime.entry_schedule_status",lambda at=None:{"allowed":at.minute<25})
    start=datetime(2026,8,20,10,tzinfo=UTC); report=service.generate_hour(start,start+timedelta(hours=1))
    assert len(json.loads(report["active_coverage"]))==1 and len(sent)==1

def test_smtp_failure_isolated_persisted_and_bounded(tmp_path):
    path=tmp_path/"db.sqlite"; setup_db(path)
    def fail(_): raise OSError("synthetic transport failure")
    service=ReportingService(path,EmailService(SMTPSettings(sender="x",recipients=("y",)),fail)); service.migrate(); start=datetime(2026,8,20,0,tzinfo=UTC)
    for _ in range(4): report=service.generate_hour(start,start+timedelta(hours=1))
    assert report["email_status"]=="FAILED" and report["email_attempts"]==3
