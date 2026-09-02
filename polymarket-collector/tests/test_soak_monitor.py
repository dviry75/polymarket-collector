import csv
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "soak_monitor.py"
spec = importlib.util.spec_from_file_location("soak_monitor", SCRIPT)
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)


def make_db(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE live_system_state(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT);
    CREATE TABLE live_reconciliation_runs(id INTEGER PRIMARY KEY,started_at TEXT,finished_at TEXT,status TEXT,gaps_count INTEGER DEFAULT 0,gaps_json TEXT,error TEXT);
    CREATE TABLE live_order_attempts(record_id TEXT PRIMARY KEY,attempt_id TEXT,phase TEXT,occurred_at TEXT,created_at TEXT,completed_at TEXT,event_id TEXT,condition_id TEXT,token_id TEXT,intent_id TEXT,position_id TEXT,operation TEXT,purpose TEXT,side TEXT,order_type TEXT,requested_price_text TEXT,requested_size_text TEXT,requested_amount_text TEXT,result_status TEXT,success INTEGER,remote_order_id TEXT,exception_type TEXT,exception_message TEXT,error_code TEXT,http_status INTEGER,normalized_json TEXT);
    CREATE TABLE live_strategy_intents(intent_id TEXT PRIMARY KEY,event_id TEXT,position_id TEXT,action TEXT,purpose TEXT,side TEXT,state TEXT,order_type TEXT,requested_amount_text TEXT,requested_shares_text TEXT,price_limit_text TEXT,filled_shares_text TEXT,average_price_text TEXT,fee_text TEXT,remaining_shares_text TEXT,remote_order_id TEXT,retry_count INTEGER,reason_code TEXT,normalized_error TEXT,created_at TEXT,submitted_at TEXT,final_at TEXT,updated_at TEXT);
    CREATE TABLE live_strategy_fills(fill_id TEXT PRIMARY KEY,intent_id TEXT,remote_trade_id TEXT,shares_text TEXT,price_text TEXT,fee_text TEXT,status TEXT,transaction_hash TEXT,matched_at TEXT,settled_at TEXT,created_at TEXT,updated_at TEXT,fee_verification_status TEXT,fee_source TEXT);
    CREATE TABLE live_audit_timeline(id INTEGER PRIMARY KEY,occurred_at TEXT,severity TEXT,category TEXT,component TEXT,source TEXT,event_id TEXT,condition_id TEXT,token_id TEXT,side TEXT,rule_id TEXT,deal_id TEXT,correlation_id TEXT,intent_id TEXT,order_id TEXT,fill_id TEXT,transaction_hash TEXT,requested_action TEXT,reason_code TEXT,previous_state TEXT,new_state TEXT,result_status TEXT,requested_amount_text TEXT,requested_shares_text TEXT,filled_shares_text TEXT,average_price_text TEXT,fees_text TEXT,remaining_shares_text TEXT,pnl_text TEXT,retry_count INTEGER,parameters_json TEXT,error_code TEXT,error_message TEXT);
    CREATE TABLE live_position_events(id INTEGER PRIMARY KEY,occurred_at TEXT,event_type TEXT,event_id TEXT,position_id TEXT,shares_text TEXT,new_state TEXT,previous_state TEXT);
    CREATE TABLE live_strategy_entry_audit(intent_id TEXT PRIMARY KEY,event_id TEXT,side TEXT,signal_price_text TEXT,signal_observed_at TEXT,revalidation_result TEXT,revalidation_ask_text TEXT,signal_to_fill_ms INTEGER,entry_validity TEXT,submitted_at TEXT,created_at TEXT,updated_at TEXT);
    CREATE TABLE live_strategy_deals(deal_id TEXT PRIMARY KEY,event_id TEXT,position_id TEXT,state TEXT,total_fees_text TEXT,realized_pnl_text TEXT,final_reason TEXT,closed_at TEXT,updated_at TEXT);
    CREATE TABLE live_strategy_positions(position_id TEXT PRIMARY KEY,event_id TEXT,token_id TEXT,state TEXT,remaining_shares_text TEXT,sellable_shares_text TEXT,dust_shares_text TEXT,stop_stage TEXT,tp_intent_id TEXT,active_exit_intent_id TEXT,resolved_winner TEXT,exit_value_text TEXT,realized_pnl_text TEXT,exit_obligation_reason TEXT,updated_at TEXT,closed_at TEXT);
    CREATE TABLE live_quarantines(id INTEGER PRIMARY KEY,status TEXT);
    """)
    c.commit(); c.close()


def args(tmp_path):
    db = tmp_path / "test.sqlite3"
    make_db(db)
    return sm.parse(["--output-dir", str(tmp_path / "out"), "--db-path", str(db),
                     "--duration-seconds", "2", "--sample-interval-seconds", "0.01",
                     "--resource-interval-seconds", "60", "--db-interval-seconds", "60",
                     "--min-free-bytes", "0", "--disk-low-bytes", "0", "--once"])


class Sink:
    def __init__(self): self.rows=[]
    def write(self,row): self.rows.append(row)


class FakeIPC:
    def __init__(self, status=None): self.value=status or {
        "strategy":{"mode":"REAL_TRADING","enabled":True,"exit_supervisor_running":True},
        "recovery":{"trading_status":"ARMED"},
        "market_ws":{"status":"CONNECTED","readiness_state":"READY","hot_path_telemetry":{}},
        "user_ws":{"status":"CONNECTED","connected":True},"provenance":{"gate_ok":True}}
    def status(self): return self.value


class FakeDB:
    def risk(self): return {}, {"active_positions":0,"managed_dust":0,"unresolved_intents":0,"open_quarantines":0}
    def incremental(self,cursors,limit):
        return {k:[] for k in ("live_reconciliation_runs","live_order_attempts","live_strategy_intents","live_strategy_fills","live_audit_timeline","live_position_events","live_strategy_entry_audit","live_strategy_deals")} | {"positions":[]}


def test_a_csv_generation_and_headers(tmp_path):
    assert sm.parse([]).sample_interval_seconds == 5
    b=sm.Budget(tmp_path,10000,0); w=sm.CSV(tmp_path/"x.csv",("a","b"),b); w.write({"a":1,"b":"two"})
    assert list(csv.DictReader((tmp_path/"x.csv").open())) == [{"a":"1","b":"two"}]


def test_b_sqlite_is_forced_read_only(tmp_path):
    db=tmp_path/"x.db"; sqlite3.connect(db).execute("CREATE TABLE x(v)").connection.commit()
    with sm.DB(db).connect() as c:
        assert c.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError): c.execute("INSERT INTO x VALUES (1)")


def test_c_ipc_and_db_code_have_no_mutating_command():
    source=SCRIPT.read_text(); ipc=source[source.index("class IPC:"):source.index("class DB:")]
    assert '"command":"STATUS"' in ipc
    assert all(f'"command":"{x}"' not in ipc for x in ("PAUSE_ENTRIES","RESUME_ENTRIES","BUY","SELL","RECONCILE"))
    db=source[source.index("class DB:"):source.index("class Metrics:")].upper()
    assert not any(x in db for x in (" INSERT "," UPDATE "," DELETE "," REPLACE "," DROP "," ALTER "))


def test_d_e_incident_detection_and_dedup(tmp_path):
    sink=Sink(); inc=sm.Incidents(sink)
    _,new1=inc.emit("HIGH","WS","DOWN",event_id="e1")
    _,new2=inc.emit("CRITICAL","WS","DOWN",event_id="e1")
    assert new1 and not new2 and len(inc.items)==1 and next(iter(inc.items.values()))["occurrence_count"]==2
    assert next(iter(inc.items.values()))["severity"]=="CRITICAL" and len(sink.rows)==2


def test_f_ring_buffer_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(sm,"service",lambda _:{}); monkeypatch.setattr(sm.IPC,"status",lambda _: {})
    m=sm.Monitor(args(tmp_path)); m.ring.extend(range(m.ring.maxlen + 100))
    assert len(m.ring)==m.ring.maxlen


def test_g_h_checkpoint_summary_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(sm,"service",lambda _:{"MainPID":1}); monkeypatch.setattr(sm.IPC,"status",lambda _: {})
    m=sm.Monitor(args(tmp_path)); m.checkpoint(2.0); s=m.summary("RUNNING")
    assert (m.dir/"checkpoints"/"02h.md").exists()
    assert {"verdict","latency_metrics","active_risk","samples"} <= s.keys()
    assert "פסק דין סופי" in m.report(s)


def test_i_safe_early_shutdown_and_required_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(sm,"service",lambda _:{"ActiveState":"active","SubState":"running","MainPID":0,"NRestarts":0})
    a=args(tmp_path); a.once=False; m=sm.Monitor(a); m.ipc=FakeIPC(); m.db=FakeDB()
    sm._STOP=True
    try: assert m.run()==0
    finally: sm._STOP=False
    summary=json.loads((m.dir/"summary.json").read_text()); assert summary["stop_reason"]=="SIGNAL"
    required={"samples.csv","system.csv","runtime.csv","market_ws.csv","user_ws.csv","reconciliation.csv","orders.csv","positions.csv","trades.csv","incidents.jsonl","summary.json","final_report.md","soak.log"}
    assert required <= {x.name for x in m.dir.iterdir()}


def test_j_pid_restart_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(sm,"service",lambda _:{"ActiveState":"active","MainPID":10,"NRestarts":0}); monkeypatch.setattr(sm.IPC,"status",lambda _: {})
    m=sm.Monitor(args(tmp_path)); m.ipc=FakeIPC()
    seq=iter(({"ActiveState":"active","MainPID":10,"NRestarts":0},{"ActiveState":"active","MainPID":11,"NRestarts":1}))
    monkeypatch.setattr(sm,"service",lambda _:next(seq)); m.sample(m.mono); m.sample(m.mono)
    assert any(x["code"]=="SERVICE_RESTART" for x in m.inc.items.values())


def test_k_trade_timeline_delta_and_jsonl(tmp_path):
    csvsink=Sink(); jsonsink=Sink(); t=sm.Timeline(csvsink,jsonsink)
    t.add("SIGNAL","2026-01-01T00:00:00Z",event_id="e"); t.add("FILL","2026-01-01T00:00:01Z",event_id="e")
    assert csvsink.rows[1]["delta_previous_ms"]==1000 and jsonsink.rows==csvsink.rows


def test_l_percentiles():
    assert sm.pct([1,2,3,4],.5)==2.5 and sm.stats([])["p99"] is None


def test_m_missing_fields_and_numeric_normalization():
    assert sm.nested({"a":{"b":2}},"a","b")==2 and sm.nested({},"a","b") is None
    assert sm.number("1.25")==1.25 and sm.number("bad") is None and sm.age_ms(None) is None


def test_n_incremental_cursor_does_not_reread(tmp_path):
    db=tmp_path/"x.db"; make_db(db); d=sm.DB(db); cur=d.baseline()
    c=sqlite3.connect(db)
    for n in range(3): c.execute("INSERT INTO live_position_events VALUES (?,?,?,?,?,?,?,?)",(n+1,"2026-01-01T00:00:00Z","E",str(n),str(n),"1","OPEN",None))
    c.commit(); c.close()
    one=d.incremental(cur,2); two=d.incremental(cur,2); three=d.incremental(cur,2)
    assert len(one["live_position_events"])==2 and len(two["live_position_events"])==1 and not three["live_position_events"]


def test_o_disk_budget_guard(tmp_path):
    b=sm.Budget(tmp_path,5,0)
    assert b.allow(5) and not b.allow(1) and b.reason=="MAX_OUTPUT_BYTES"


def test_p_q_terminal_dust_not_active_and_open_exit_are_risk(tmp_path):
    db=tmp_path/"x.db"; make_db(db); c=sqlite3.connect(db)
    rows=[("d","e","t","DUST","0","0","0",None,None,None,None,None,None,None,"2026", "2026"),
          ("o","e2","t2","OPEN","1","1","0",None,None,None,None,None,None,None,"2026",None),
          ("x","e3","t3","EXITING","1","1","0",None,None,None,None,None,None,None,"2026",None)]
    c.executemany("INSERT INTO live_strategy_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",rows); c.commit(); c.close()
    _,risk=sm.DB(db).risk(); assert risk["managed_dust"]==0 and risk["active_positions"]==2
