#!/usr/bin/env python3
"""Lightweight 72h LIVE soak monitor. Trader access is strictly read-only."""
from __future__ import annotations
import argparse,csv,hashlib,json,math,os,platform,signal,socket,sqlite3,statistics,subprocess,threading,time,uuid
from collections import Counter,defaultdict,deque
from datetime import datetime,timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

FINAL={"FILLED","PARTIAL_FINAL","ZERO_FILL","CANCELED","REJECTED","FAILED","SETTLED","REDEEMED"}
ACTIVE={"OPEN","TP_OPEN","EXITING","EXIT_RECONCILIATION_REQUIRED","QUARANTINED"}
SEV={"INFO":0,"WARNING":1,"HIGH":2,"CRITICAL":3}
STATE_KEYS=("kill_switch","pause_entries","pause_owner","pause_cause","pause_reason","pause_state",
"release_policy","strategy_readiness","strategy_block_reason","reconciliation_readiness",
"reconciliation_block_reason","live_blocked_by_reconciliation","last_successful_reconciliation_at",
"order_heartbeat_status","last_successful_heartbeat_at","market_ws_status","user_ws_status",
"recovery_status","recovery_engine_status","operator_action_required","global_entry_halt_required",
"incident_scope")
FILES={
"samples.csv":("timestamp_utc","timestamp_israel","elapsed_seconds","sample_latency_ms","sample_loop_duration_ms","deadline_lateness_ms",
"missed_deadline","service_active","trader_pid","trading_status","strategy_readiness","pause_entries",
"reconciliation_readiness","active_positions","managed_dust","unresolved_intents","open_quarantines","incidents"),
"system.csv":("timestamp_utc","elapsed_seconds","load_1m","load_5m","load_15m","cpu_count","mem_total_kb",
"mem_available_kb","swap_total_kb","swap_free_kb","disk_free_bytes","db_bytes","wal_bytes","db_growth_bytes",
"monitor_output_bytes","monitor_cpu_percent","monitor_rss_kb","trader_cpu_percent","trader_rss_kb",
"trader_vsz_kb","trader_threads","trader_fds"),
"db_latency.csv":("timestamp_utc","risk_query_ms","incremental_query_ms","total_query_ms","rows_read"),
"runtime.csv":("timestamp_utc","elapsed_seconds","service_active","service_substate","pid","pid_changed",
"nrestarts","mode","enabled","readiness","block_reason","trading_status","pause_entries","heartbeat_status",
"provenance_ok","status_snapshot_age_ms","event_loop_current_ms","event_loop_p50_ms","event_loop_p95_ms","event_loop_p99_ms",
"event_loop_max_ms","event_loop_gt_100ms","event_loop_gt_500ms","event_loop_gt_1000ms",
"event_loop_gt_5000ms","sampling_latency_ms","missed_deadlines","exit_supervisor_running",
"exit_supervisor_runs","exit_supervisor_worst_eval_latency_ms","exit_supervisor_max_concurrency",
"exit_supervisor_concurrency_limit","exit_supervisor_deferred","exit_supervisor_pending_initial_eval",
"exit_rest_fallbacks","exit_rest_failures"),
"market_ws.csv":("timestamp_utc","status","connected","stale","readiness","generation","reconnect_count",
"reconnect_attempts","disconnect_count","last_message_age_ms","receive_latency_ms","exchange_age_ms",
"exchange_age_p50_ms","exchange_age_p95_ms","exchange_age_p99_ms","rejected_frames","stale_frames",
"out_of_order_frames","duplicate_frames","best_price_mismatches","alignment_pending","alignment_recoveries",
"subscription_status","required_assets","queue_depth","queue_capacity","max_queue_depth","saturations","frames_dropped",
"frames_discarded","processing_p95_ms","ingress_wait_p95_ms","persistence_queue_depth","persistence_failures","rejection_reasons","not_ready_total_seconds","not_ready_max_seconds"),
"user_ws.csv":("timestamp_utc","status","connected","stale","reconnect_count","reconnect_attempts",
"last_message_age_ms","last_ping_at","last_pong_at","order_events","trade_events","queue_depth",
"dropped_events","persistence_failures","last_error"),
"reconciliation.csv":("run_id","started_at","finished_at","duration_ms","status","gap_count","gap_types",
"repair_count","repair_types","error","retry_count","backoff_seconds","readiness_before","readiness_after",
"live_blocked","fingerprint","repeat_count"),
"orders.csv":("rowid","occurred_at","attempt_id","phase","operation","purpose","event_id","position_id",
"intent_id","side","order_type","requested_price","requested_size","requested_amount","result_status",
"success","remote_order_id","error_class","error_code","http_status","adapter_latency_ms"),
"positions.csv":("position_id","event_id","token_id","state","remaining_shares","sellable_shares",
"dust_shares","stop_stage","tp_intent_id","active_exit_intent_id","resolved_winner","exit_value",
"realized_pnl","exit_obligation_reason","updated_at","classification"),
"trades.csv":("timestamp_utc","timestamp_israel","node","event_id","position_id","intent_id","side",
"price","shares","state","reason","source","delta_previous_ms","classification")}
_STOP=False
def on_signal(*_):
 global _STOP;_STOP=True
signal.signal(signal.SIGINT,on_signal);signal.signal(signal.SIGTERM,on_signal)
def now(): return datetime.now(timezone.utc)
def iso(dt=None): return (dt or now()).isoformat().replace("+00:00","Z")
def israel(value=None):
 if isinstance(value,str):
  try:value=datetime.fromisoformat(value.replace("Z","+00:00"))
  except ValueError:value=now()
 return (value or now()).astimezone(ZoneInfo("Asia/Jerusalem")).isoformat()
def epoch(v):
 try:return datetime.fromisoformat(str(v).replace("Z","+00:00")).timestamp() if v else None
 except (ValueError,TypeError):return None
def age_ms(v):
 t=epoch(v)
 return round(max(0,(time.time()-t)*1000),3) if t is not None else None
def number(v,default=None):
 try:return float(v)
 except (ValueError,TypeError):return default
def nested(d,*keys,default=None):
 for k in keys:
  if not isinstance(d,dict):return default
  d=d.get(k)
 return default if d is None else d
def pct(values,p):
 v=sorted(x for x in values if x is not None and math.isfinite(x))
 if not v:return None
 r=(len(v)-1)*p;lo=int(r);hi=math.ceil(r)
 return round(v[lo] if lo==hi else v[lo]+(v[hi]-v[lo])*(r-lo),3)
def stats(values):
 v=[float(x) for x in values if x is not None and math.isfinite(float(x))]
 return {"count":len(v),"min":round(min(v),3) if v else None,"p50":pct(v,.5),"p95":pct(v,.95),
 "p99":pct(v,.99),"max":round(max(v),3) if v else None}
def atomic(path,payload):
 tmp=path.with_suffix(path.suffix+".tmp");tmp.write_text(json.dumps(payload,indent=2,sort_keys=True,default=str))
 os.replace(tmp,path)
def command(argv,timeout=5):
 try:return subprocess.run(argv,capture_output=True,text=True,timeout=timeout,check=False).stdout.strip()
 except Exception:return ""
def service(name):
 out={}
 for line in command(["systemctl","show",name,"-p","ActiveState","-p","SubState","-p","MainPID",
                      "-p","NRestarts","-p","ExecMainStartTimestamp"]).splitlines():
  k,_,v=line.partition("=");out[k]=v
 for k in ("MainPID","NRestarts"):
  try:out[k]=int(out.get(k) or 0)
  except ValueError:out[k]=0
 return out
def proc(pid):
 if not pid:return {}
 try:
  s=Path(f"/proc/{pid}/stat").read_text().split();status=Path(f"/proc/{pid}/status").read_text()
  out={"cpu":(int(s[13])+int(s[14]))/os.sysconf("SC_CLK_TCK"),"threads":int(s[19])}
  for line in status.splitlines():
   if line.startswith("VmRSS:"):out["rss"]=int(line.split()[1])
   if line.startswith("VmSize:"):out["vsz"]=int(line.split()[1])
  try:out["fds"]=len(os.listdir(f"/proc/{pid}/fd"))
  except OSError:out["fds"]=None
  return out
 except (OSError,ValueError,IndexError):return {}
def mem():
 out={}
 try:
  for line in Path("/proc/meminfo").read_text().splitlines():
   k,v=line.split(":",1)
   if k in {"MemTotal","MemAvailable","SwapTotal","SwapFree"}:out[k]=int(v.split()[0])
 except OSError:pass
 return out

class Budget:
 def __init__(self,root,max_bytes,min_free):
  self.root=root;self.max=max_bytes;self.floor=min_free;self.written=0;self.reason=""
 def allow(self,n):
  try:s=os.statvfs(self.root);free=s.f_bavail*s.f_frsize
  except OSError:free=self.floor+n
  if self.written+n>self.max:self.reason="MAX_OUTPUT_BYTES";return False
  if free-n<self.floor:self.reason="DISK_FREE_FLOOR";return False
  self.written+=n;return True
class CSV:
 def __init__(self,path,fields,budget):
  self.path=path;self.fields=fields;self.budget=budget
  if not path.exists() or not path.stat().st_size:
   with path.open("a",newline="") as f:csv.DictWriter(f,fieldnames=fields).writeheader()
 def write(self,row):
  import io
  b=io.StringIO();csv.DictWriter(b,fieldnames=self.fields,extrasaction="ignore").writerow({k:row.get(k,"") for k in self.fields})
  data=b.getvalue()
  if not self.budget.allow(len(data.encode())):raise OSError(self.budget.reason)
  with self.path.open("a",newline="") as f:f.write(data);f.flush()
class JSONL:
 def __init__(self,path,budget):self.path=path;self.budget=budget;path.touch(exist_ok=True)
 def write(self,row):
  data=json.dumps(row,sort_keys=True,separators=(",",":"),default=str)+"\n"
  if not self.budget.allow(len(data.encode())):raise OSError(self.budget.reason)
  with self.path.open("a") as f:f.write(data);f.flush()
class Logger:
 def __init__(self,path,budget):self.path=path;self.budget=budget;path.touch(exist_ok=True)
 def log(self,level,msg):
  if self.path.stat().st_size>20_000_000:
   old=self.path.with_suffix(".log.1")
   if old.exists():old.unlink()
   self.path.replace(old)
  line=f"{iso()} [{level}] {str(msg)[:4000]}\n"
  if self.budget.allow(len(line.encode())):
   with self.path.open("a") as f:f.write(line);f.flush()

class IPC:
 def __init__(self,path,timeout):self.path=path;self.timeout=timeout
 def status(self):
  req=json.dumps({"request_id":uuid.uuid4().hex,"command":"STATUS","payload":{}}).encode()+b"\n"
  with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as c:
   c.settimeout(self.timeout);c.connect(self.path);c.sendall(req);data=bytearray()
   while b"\n" not in data:
    part=c.recv(65536)
    if not part:break
    data.extend(part)
    if len(data)>4*1024*1024:raise RuntimeError("IPC_STATUS_TOO_LARGE")
  result=json.loads(bytes(data).split(b"\n",1)[0])
  if not result.get("ok"):raise RuntimeError(str(result.get("error")))
  return result.get("result") or {}

class DB:
 def __init__(self,path):self.path=path
 def connect(self):
  c=sqlite3.connect(f"file:{Path(self.path).resolve()}?mode=ro",uri=True,timeout=4)
  c.row_factory=sqlite3.Row;c.execute("PRAGMA query_only=ON");return c
 def baseline(self):
  tables=("live_reconciliation_runs","live_order_attempts","live_strategy_intents","live_strategy_fills",
          "live_audit_timeline","live_position_events","live_strategy_entry_audit","live_strategy_deals")
  with self.connect() as c:return {t:int(c.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {t}").fetchone()[0]) for t in tables}
 def risk(self):
  with self.connect() as c:
   marks=",".join("?" for _ in STATE_KEYS)
   state={r["key"]:r["value"] for r in c.execute(f"SELECT key,value FROM live_system_state WHERE key IN ({marks})",STATE_KEYS)}
   active=c.execute("SELECT COUNT(*) FROM live_strategy_positions WHERE state IN ('OPEN','TP_OPEN','EXITING','EXIT_RECONCILIATION_REQUIRED','QUARANTINED')").fetchone()[0]
   dust=c.execute("SELECT COUNT(*) FROM live_strategy_positions WHERE state='DUST' AND closed_at IS NULL AND CAST(sellable_shares_text AS REAL)>0").fetchone()[0]
   unresolved=c.execute("SELECT COUNT(*) FROM live_strategy_intents WHERE state NOT IN ('FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED','REJECTED','FAILED','SETTLED','REDEEMED')").fetchone()[0]
   quarantine=c.execute("SELECT COUNT(*) FROM live_quarantines WHERE status='OPEN'").fetchone()[0]
  return state,{"active_positions":active,"managed_dust":dust,"unresolved_intents":unresolved,"open_quarantines":quarantine}
 def incremental(self,cursors,limit):
  q={
  "live_reconciliation_runs":"SELECT rowid AS _cursor,* FROM live_reconciliation_runs WHERE rowid>? ORDER BY rowid LIMIT ?",
  "live_order_attempts":"SELECT rowid AS _cursor,record_id,attempt_id,phase,occurred_at,created_at,completed_at,event_id,condition_id,token_id,intent_id,position_id,operation,purpose,side,order_type,requested_price_text,requested_size_text,requested_amount_text,result_status,success,remote_order_id,exception_type,exception_message,error_code,http_status,normalized_json FROM live_order_attempts WHERE rowid>? ORDER BY rowid LIMIT ?",
  "live_strategy_intents":"SELECT rowid AS _cursor,intent_id,event_id,position_id,action,purpose,side,state,order_type,requested_amount_text,requested_shares_text,price_limit_text,filled_shares_text,average_price_text,fee_text,remaining_shares_text,remote_order_id,retry_count,reason_code,normalized_error,created_at,submitted_at,final_at,updated_at FROM live_strategy_intents WHERE rowid>? ORDER BY rowid LIMIT ?",
  "live_strategy_fills":"SELECT f.rowid AS _cursor,f.fill_id,f.intent_id,f.remote_trade_id,f.shares_text,f.price_text,f.fee_text,f.status,f.transaction_hash,f.matched_at,f.settled_at,f.created_at,f.fee_verification_status,f.fee_source,i.event_id,i.position_id,i.action,i.purpose,i.side FROM live_strategy_fills f LEFT JOIN live_strategy_intents i ON i.intent_id=f.intent_id WHERE f.rowid>? ORDER BY f.rowid LIMIT ?",
  "live_audit_timeline":"SELECT rowid AS _cursor,* FROM live_audit_timeline WHERE rowid>? ORDER BY rowid LIMIT ?",
  "live_position_events":"SELECT rowid AS _cursor,* FROM live_position_events WHERE rowid>? ORDER BY rowid LIMIT ?",
  "live_strategy_entry_audit":"SELECT rowid AS _cursor,* FROM live_strategy_entry_audit WHERE rowid>? ORDER BY rowid LIMIT ?",
  "live_strategy_deals":"SELECT rowid AS _cursor,* FROM live_strategy_deals WHERE rowid>? ORDER BY rowid LIMIT ?"}
  out={}
  with self.connect() as c:
   for t,sql in q.items():
    rows=[dict(x) for x in c.execute(sql,(cursors.get(t,0),limit))]
    out[t]=rows
    if rows:cursors[t]=max(x["_cursor"] for x in rows)
   out["positions"]=[dict(x) for x in c.execute("SELECT position_id,event_id,token_id,state,remaining_shares_text,sellable_shares_text,dust_shares_text,stop_stage,tp_intent_id,active_exit_intent_id,resolved_winner,exit_value_text,realized_pnl_text,exit_obligation_reason,updated_at,closed_at FROM live_strategy_positions WHERE state IN ('OPEN','TP_OPEN','EXITING','EXIT_RECONCILIATION_REQUIRED','QUARANTINED','DUST') ORDER BY updated_at DESC LIMIT 500")]
  return out

class Metrics:
 def __init__(self):self.v=defaultdict(lambda:deque(maxlen=20000));self.c=Counter()
 def add(self,k,v):
  x=number(v)
  if x is not None and math.isfinite(x):self.v[k].append(x)
 def summary(self,k):return stats(list(self.v[k]))
class Incidents:
 def __init__(self,stream,max_items=5000):self.stream=stream;self.max=max_items;self.items={}
 def emit(self,severity,category,code,**kw):
  raw="|".join(str(kw.get(k) or "") for k in ("event_id","position_id","intent_id"))
  fp=hashlib.sha256((code+"|"+raw).encode()).hexdigest()[:20];new=fp not in self.items
  if new and len(self.items)>=self.max:fp=hashlib.sha256(b"INCIDENT_CAP").hexdigest()[:20];new=fp not in self.items
  if new:self.items[fp]={"incident_id":uuid.uuid4().hex,"fingerprint":fp,"timestamp":iso(),"first_seen":iso(),
   "last_seen":iso(),"occurrence_count":0,"severity":severity,"category":category,"code":code,
   "event_id":kw.get("event_id"),"position_id":kw.get("position_id"),"intent_id":kw.get("intent_id")}
  item=self.items[fp];item["last_seen"]=iso();item["occurrence_count"]+=1
  if SEV.get(severity,0)>SEV.get(item["severity"],0):item["severity"]=severity
  record={**item,"current_runtime_state":kw.get("runtime") or {},"evidence":kw.get("evidence") or {},
          "relevant_metrics":kw.get("metrics") or {}}
  count=item["occurrence_count"]
  if new or count & (count-1)==0:self.stream.write(record)
  return record,new
class Timeline:
 def __init__(self,writer,stream=None):self.writer=writer;self.stream=stream;self.last={}
 def add(self,node,timestamp=None,classification="CLEAN",**kw):
  timestamp=timestamp or iso();key=str(kw.get("position_id") or kw.get("event_id") or kw.get("intent_id") or "unknown")
  at=epoch(timestamp);delta=round((at-self.last[key])*1000,3) if at is not None and key in self.last else ""
  if at is not None:self.last[key]=at
  row={"timestamp_utc":timestamp,"timestamp_israel":israel(timestamp),"node":node,
   "delta_previous_ms":delta,"classification":classification,**kw}
  self.writer.write(row)
  if self.stream:self.stream.write(row)

class Monitor:
 def __init__(self,a):
  self.a=a;self.start=now();self.mono=time.monotonic();self.run_id=a.run_id or self.start.strftime("%Y%m%d_%H%M%S")+"_72h"
  self.dir=Path(a.resume or a.run_dir or Path(a.output_dir)/self.run_id).resolve()
  self.dir.mkdir(parents=True,exist_ok=True);(self.dir/"checkpoints").mkdir(exist_ok=True);(self.dir/"incident_snapshots").mkdir(exist_ok=True)
  Path(a.output_dir).mkdir(parents=True,exist_ok=True);(Path(a.output_dir)/"latest_run.txt").write_text(str(self.dir)+"\n");(self.dir/"monitor.pid").write_text(str(os.getpid())+"\n")
  self.budget=Budget(self.dir,a.max_output_bytes,a.min_free_bytes)
  self.out={f:CSV(self.dir/f,fields,self.budget) for f,fields in FILES.items()}
  self.inc=Incidents(JSONL(self.dir/"incidents.jsonl",self.budget));self.trade_stream=JSONL(self.dir/"trade_timelines.jsonl",self.budget);self.timeline=Timeline(self.out["trades.csv"],self.trade_stream)
  self.log=Logger(self.dir/"soak.log",self.budget);self.db=DB(a.db_path);self.ipc=IPC(a.socket_path,a.ipc_timeout)
  self.status_lock=threading.Lock();self.status_cache={};self.status_cache_at=0.0;self.status_error="";self.poll_stop=threading.Event();self.poll_thread=None
  self.metrics=Metrics();self.cursors=self.db.baseline();self.risk={"active_positions":0,"managed_dust":0,"unresolved_intents":0,"open_quarantines":0}
  self.state={};self.prev={};self.ring=deque(maxlen=max(1,int(a.ring_buffer_seconds/a.sample_interval_seconds)))
  self.captures={};self.samples=0;self.missed=0;self.checkpoints=[];self.repeats=Counter();self.cpu_prev={}
  self.db0=self.size(a.db_path);self.stop_reason="";self.down_since=None;self.downtime=0.0;self.metadata=self.make_metadata();atomic(self.dir/"metadata.json",self.metadata)
 def size(self,p):
  try:return os.path.getsize(p)
  except OSError:return 0
 def free(self):
  s=os.statvfs(self.dir);return s.f_bavail*s.f_frsize
 def make_metadata(self):
  svc=service(self.a.trader_service);git=self.a.git_root
  try:st=self.ipc.status();self.status_cache=st;self.status_cache_at=time.monotonic()
  except Exception as e:st={};self.status_error=str(e)
  strategy=st.get("strategy") or {}
  return {"run_id":self.run_id,"started_at_utc":iso(self.start),"started_at_israel":israel(self.start),
   "requested_duration_seconds":self.a.duration_seconds,"sampling_interval_seconds":self.a.sample_interval_seconds,
   "checkpoints_hours":self.a.checkpoints,"hostname":platform.node(),"monitor_pid":os.getpid(),
   "service_pid":svc.get("MainPID"),"service_start_time":svc.get("ExecMainStartTimestamp"),
   "git_sha":command(["git","-C",git,"rev-parse","HEAD"]),"branch":command(["git","-C",git,"branch","--show-current"]),
   "git_dirty":bool(command(["git","-C",git,"status","--porcelain"])),"db_path":str(Path(self.a.db_path).resolve()),
   "db_initial_size":self.db0,"free_disk_bytes":self.free(),"cpu_count":os.cpu_count(),"ram_kb":mem().get("MemTotal"),
   "mode":strategy.get("mode"),"strategy_enabled":strategy.get("enabled"),
   "strategy_version":strategy.get("version"),"config_hash":strategy.get("config_hash"),
   "provenance":st.get("provenance") or {},
   "status":"RUNNING","read_only":{"ipc":["STATUS"],"sqlite":"mode=ro;query_only=ON","writes":str(self.dir)}}
 def poll_status(self):
  while not self.poll_stop.wait(self.a.status_interval_seconds):
   try:
    value=self.ipc.status()
    with self.status_lock:self.status_cache=value;self.status_cache_at=time.monotonic();self.status_error=""
   except Exception as e:
    with self.status_lock:self.status_error=str(e)
 def cached_status(self):
  with self.status_lock:return self.status_cache,self.status_cache_at,self.status_error
 def incident(self,severity,category,code,**kw):
  rec,new=self.inc.emit(severity,category,code,runtime=self.prev,**kw)
  if new:
   self.captures[rec["fingerprint"]]={"remaining":max(1,int(self.a.post_incident_seconds/self.a.sample_interval_seconds)),
    "data":{"incident":rec,"before":list(self.ring),"after":[]}}
   self.log.log(severity,code)
 def capture(self,sample):
  done=[]
  for fp,x in self.captures.items():
   x["data"]["after"].append(sample);x["remaining"]-=1
   if x["remaining"]<=0:atomic(self.dir/"incident_snapshots"/(fp+".json"),x["data"]);done.append(fp)
  for fp in done:self.captures.pop(fp)
 def cpu(self,key,r,tm):
  if "cpu" not in r:return None
  old=self.cpu_prev.get(key);self.cpu_prev[key]=(tm,r["cpu"])
  return round(100*(r["cpu"]-old[1])/(tm-old[0]),3) if old and tm>old[0] else None
 def sample(self,expected):
  began=time.monotonic();ts=iso();svc=service(self.a.trader_service);pid=svc.get("MainPID",0);active=svc.get("ActiveState")=="active"
  st,cache_at,status_error=self.cached_status()
  if status_error and not st:self.incident("CRITICAL","SERVICE","STATUS_UNAVAILABLE",evidence={"error":status_error})
  latency=(time.monotonic()-began)*1000;late=max(0,(began-expected)*1000);missed=late>self.a.sample_interval_seconds*500
  self.samples+=1;self.missed+=int(missed);strategy=st.get("strategy") or {};recovery=st.get("recovery") or {}
  mw=st.get("market_ws") or {};uw=st.get("user_ws") or {};telem=mw.get("hot_path_telemetry") or {}
  loop=telem.get("event_loop_lag_ms") or {};buckets=telem.get("event_loop_lag_buckets") or {};prov=st.get("provenance") or {}
  rt={"timestamp_utc":ts,"elapsed_seconds":round(began-self.mono,3),"service_active":active,
   "service_substate":svc.get("SubState"),"pid":pid,"pid_changed":bool(self.prev.get("pid") and self.prev["pid"]!=pid),
   "nrestarts":svc.get("NRestarts"),"mode":strategy.get("mode"),"enabled":strategy.get("enabled"),
   "readiness":strategy.get("readiness"),"block_reason":strategy.get("block_reason"),
   "trading_status":recovery.get("trading_status"),"pause_entries":strategy.get("pause_entries"),
   "heartbeat_status":strategy.get("heartbeat_status"),"provenance_ok":prov.get("gate_ok"),
   "status_snapshot_age_ms":round((time.monotonic()-cache_at)*1000,3) if cache_at else None,
   "event_loop_current_ms":loop.get("current"),"event_loop_p50_ms":loop.get("p50"),
   "event_loop_p95_ms":loop.get("p95"),"event_loop_p99_ms":loop.get("p99"),"event_loop_max_ms":loop.get("max"),
   "event_loop_gt_100ms":buckets.get("gt_100ms"),"event_loop_gt_500ms":buckets.get("gt_500ms"),
   "event_loop_gt_1000ms":buckets.get("gt_1000ms"),"event_loop_gt_5000ms":buckets.get("gt_5000ms"),
   "sampling_latency_ms":round(latency,3),"missed_deadlines":self.missed,
   "exit_supervisor_running":strategy.get("exit_supervisor_running"),
   "exit_supervisor_runs":strategy.get("exit_supervisor_runs"),
   "exit_supervisor_worst_eval_latency_ms":strategy.get("exit_supervisor_worst_eval_latency_ms"),
   "exit_supervisor_max_concurrency":strategy.get("exit_supervisor_max_observed_concurrency"),
   "exit_supervisor_concurrency_limit":strategy.get("exit_supervisor_max_concurrent_book_fetches"),
   "exit_supervisor_deferred":strategy.get("exit_supervisor_deferred_low_priority"),
   "exit_supervisor_pending_initial_eval":strategy.get("exit_supervisor_pending_initial_eval"),
   "exit_rest_fallbacks":strategy.get("exit_rest_fallbacks"),"exit_rest_failures":strategy.get("exit_rest_failures")}
  self.out["runtime.csv"].write(rt);ex=telem.get("exchange_age_at_reader_ms") or {};books=mw.get("books") or {}
  mwrow={"timestamp_utc":ts,"status":mw.get("status"),"connected":mw.get("status")=="CONNECTED","stale":mw.get("stale"),
   "readiness":mw.get("readiness_state"),"generation":nested(telem,"generation_timing","generation"),
   "reconnect_count":telem.get("connection_generations_total"),"reconnect_attempts":telem.get("reconnect_attempts_total"),
   "disconnect_count":telem.get("disconnects_total"),"last_message_age_ms":age_ms(mw.get("last_message_at")),
   "receive_latency_ms":mw.get("receive_latency_ms"),
   "exchange_age_ms":mw.get("exchange_age_ms"),"exchange_age_p50_ms":ex.get("p50"),
   "exchange_age_p95_ms":ex.get("p95"),"exchange_age_p99_ms":ex.get("p99"),
   "rejected_frames":mw.get("rejected_frames"),"stale_frames":(mw.get("rejection_reasons") or {}).get("STALE_EXCHANGE_TIMESTAMP"),
   "out_of_order_frames":mw.get("out_of_order_frames"),"duplicate_frames":mw.get("duplicate_frames"),
   "best_price_mismatches":mw.get("best_price_mismatches"),"alignment_pending":sum(bool(x.get("alignment_pending")) for x in books.values()),
   "alignment_recoveries":mw.get("best_price_alignment_recoveries"),"subscription_status":mw.get("subscription_status"),
   "required_assets":len(mw.get("subscribed_asset_ids") or []),"queue_depth":mw.get("ingress_queue_depth"),
   "queue_capacity":mw.get("ingress_queue_capacity"),
   "max_queue_depth":mw.get("max_ingress_queue_depth"),"saturations":mw.get("ingress_queue_saturations"),
   "frames_dropped":mw.get("critical_triggers_dropped"),"frames_discarded":mw.get("ingress_market_frames_discarded"),
   "processing_p95_ms":nested(telem,"market_processing_ms","p95"),"ingress_wait_p95_ms":nested(telem,"ingress_queue_wait_ms","p95"),
   "persistence_queue_depth":mw.get("persistence_queue_depth"),"persistence_failures":mw.get("persistence_failures"),
   "rejection_reasons":json.dumps(mw.get("rejection_reasons") or {},sort_keys=True),
   "not_ready_total_seconds":mw.get("not_ready_total_seconds"),"not_ready_max_seconds":mw.get("not_ready_max_seconds")}
  self.out["market_ws.csv"].write(mwrow)
  uwrow={"timestamp_utc":ts,"status":uw.get("status"),"connected":uw.get("connected"),"stale":uw.get("stale"),
   "reconnect_count":uw.get("reconnect_count"),"reconnect_attempts":uw.get("reconnect_attempts"),
   "last_message_age_ms":age_ms(uw.get("last_message_at")),"last_ping_at":uw.get("last_ping_at"),"last_pong_at":uw.get("last_pong_at"),
   "order_events":uw.get("order_events_received"),"trade_events":uw.get("trade_events_received"),
   "queue_depth":uw.get("event_queue_depth"),"dropped_events":uw.get("event_queue_dropped"),
   "persistence_failures":uw.get("event_persistence_failures"),"last_error":uw.get("last_error")}
  self.out["user_ws.csv"].write(uwrow)
  sample_row={"timestamp_utc":ts,"timestamp_israel":israel(),"elapsed_seconds":round(began-self.mono,3),
   "sample_latency_ms":round(latency,3),"deadline_lateness_ms":round(late,3),"missed_deadline":missed,
   "service_active":active,"trader_pid":pid,"trading_status":recovery.get("trading_status"),
   "strategy_readiness":strategy.get("readiness"),"pause_entries":strategy.get("pause_entries"),
   "reconciliation_readiness":strategy.get("reconciliation_readiness"),**self.risk,"incidents":len(self.inc.items)}
  snap={"timestamp":ts,"runtime":rt,"market_ws":mwrow,"user_ws":uwrow,"risk":self.risk};self.ring.append(snap);self.capture(snap)
  if not active:
   if self.down_since is None:self.down_since=time.monotonic()
   self.incident("CRITICAL","SERVICE","SERVICE_DOWN",evidence=svc)
  elif self.down_since is not None:
   self.downtime+=time.monotonic()-self.down_since;self.down_since=None
  if rt["pid_changed"]:self.incident("CRITICAL","SERVICE","SERVICE_RESTART",evidence=svc);self.metrics.c["service_restarts"]+=1
  if prov and not prov.get("gate_ok",True):self.incident("CRITICAL","PROVENANCE","PROVENANCE_FAILURE",evidence=prov)
  if strategy.get("heartbeat_status") not in {None,"OK"}:self.incident("CRITICAL","HEARTBEAT","ORDER_HEARTBEAT_FAILURE",evidence=rt)
  if mw and mw.get("status")!="CONNECTED":self.incident("HIGH","MARKET_WS","MARKET_WS_DISCONNECT",evidence=mwrow)
  if mw and mw.get("readiness_state")!="READY":self.incident("HIGH","MARKET_WS","MARKET_WS_NOT_READY",evidence=mwrow)
  if mw.get("stale"):self.incident("HIGH","MARKET_WS","MARKET_DATA_STALE",evidence=mwrow)
  cap=number(mw.get("ingress_queue_capacity"));depth=number(mw.get("ingress_queue_depth"))
  if cap and depth is not None and depth/cap>=self.a.ws_backlog_ratio:self.incident("HIGH","MARKET_WS","MARKET_WS_BACKLOG",evidence=mwrow)
  if uw and uw.get("status")!="CONNECTED":self.incident("HIGH","USER_WS","USER_WS_DISCONNECT",evidence=uwrow)
  for obj,prefix,field in ((mw,"MARKET_WS","reconnect_attempts_total"),(uw,"USER_WS","reconnect_count")):
   cur=int(obj.get(field) or 0);key=prefix.lower()+"_reconnect";old=int(self.prev.get(key) or cur)
   if cur>old:self.incident("WARNING",prefix,prefix+"_RECONNECT",evidence={"delta":cur-old})
   self.prev[key]=cur
  dropped=int(uw.get("event_queue_dropped") or 0);old=int(self.prev.get("user_ws_dropped") or dropped)
  if dropped>old:self.incident("HIGH","USER_WS","USER_WS_EVENTS_DROPPED",evidence={"delta":dropped-old})
  self.prev["user_ws_dropped"]=dropped
  for field,code,severity in (("best_price_mismatches","BEST_PRICE_MISMATCH","HIGH"),("ingress_queue_saturations","INGRESS_SATURATION","CRITICAL")):
   cur=int(mw.get(field) or 0);old=int(self.prev.get(field) or cur)
   if cur>old:self.incident(severity,"MARKET_WS",code,evidence={"delta":cur-old})
   self.prev[field]=cur
  if strategy.get("exit_supervisor_running") is False:self.incident("CRITICAL","EXIT_SUPERVISOR","EXIT_SUPERVISOR_DOWN",evidence=rt)
  if number(strategy.get("exit_supervisor_pending_initial_eval"),0)>0 and number(strategy.get("exit_supervisor_worst_eval_latency_ms"),0)>self.a.exit_supervisor_sla_ms:self.incident("HIGH","EXIT_SUPERVISOR","EXIT_SUPERVISOR_STARVATION",evidence=rt)
  for field,code in (("exit_rest_failures","EXIT_REST_FAILURE"),("critical_triggers_dropped","CRITICAL_TRIGGER_DROPPED")):
   cur=int(strategy.get(field) or 0);old=int(self.prev.get(field) or cur)
   if cur>old:self.incident("CRITICAL","EXIT_SUPERVISOR",code,evidence={"delta":cur-old,**rt})
   self.prev[field]=cur
  if number(loop.get("current"),0)>self.a.event_loop_lag_high_ms:self.incident("HIGH","RUNTIME","EVENT_LOOP_LAG_HIGH",evidence=loop)
  sample_duration=(time.monotonic()-began)*1000;sample_row["sample_loop_duration_ms"]=round(sample_duration,3)
  self.out["samples.csv"].write(sample_row)
  self.metrics.add("event_loop_lag_ms",loop.get("current"));self.metrics.add("sampling_loop_latency_ms",latency);self.metrics.add("sample_loop_duration_ms",sample_duration)
  self.prev.update({"pid":pid,"nrestarts":svc.get("NRestarts")})
 def resources(self):
  tm=time.monotonic();svc=service(self.a.trader_service);tr=proc(svc.get("MainPID"));me=proc(os.getpid());mi=mem();load=os.getloadavg()
  row={"timestamp_utc":iso(),"elapsed_seconds":round(tm-self.mono,3),"load_1m":load[0],"load_5m":load[1],"load_15m":load[2],
   "cpu_count":os.cpu_count(),"mem_total_kb":mi.get("MemTotal"),"mem_available_kb":mi.get("MemAvailable"),
   "swap_total_kb":mi.get("SwapTotal"),"swap_free_kb":mi.get("SwapFree"),"disk_free_bytes":self.free(),
   "db_bytes":self.size(self.a.db_path),"wal_bytes":self.size(self.a.db_path+"-wal"),
   "db_growth_bytes":self.size(self.a.db_path)-self.db0,"monitor_output_bytes":self.budget.written,
   "monitor_cpu_percent":self.cpu("monitor",me,tm),"monitor_rss_kb":me.get("rss"),
   "trader_cpu_percent":self.cpu("trader",tr,tm),"trader_rss_kb":tr.get("rss"),"trader_vsz_kb":tr.get("vsz"),
   "trader_threads":tr.get("threads"),"trader_fds":tr.get("fds")}
  self.out["system.csv"].write(row)
  for k in ("monitor_cpu_percent","monitor_rss_kb","trader_cpu_percent","trader_rss_kb","db_growth_bytes"):self.metrics.add(k,row.get(k))
  if number(row["trader_cpu_percent"],0)>self.a.cpu_high_percent:self.incident("HIGH","RESOURCE","CPU_HIGH",evidence=row)
  if number(row["trader_rss_kb"],0)>self.a.memory_high_mb*1024:self.incident("HIGH","RESOURCE","MEMORY_HIGH",evidence=row)
  if row["disk_free_bytes"]<self.a.disk_low_bytes:self.incident("CRITICAL","RESOURCE","DISK_LOW",evidence=row)
  if row["db_growth_bytes"]>self.a.db_growth_high_bytes:self.incident("HIGH","RESOURCE","DB_GROWTH_HIGH",evidence=row)
 def error_class(self,r):
  text=" ".join(str(r.get(k) or "").lower() for k in ("result_status","error_code","exception_type","exception_message"));http=int(r.get("http_status") or 0)
  if r.get("success"):return "success"
  if http==429:return "rate_limit_429"
  if http>=500:return "http_5xx"
  if http>=400:return "http_4xx"
  for marker,name in (("timeout","timeout"),("transport","transport_error"),("insufficient","insufficient_balance"),
   ("allowance","allowance"),("signature","signature_auth"),("auth","signature_auth"),("minimum","min_order"),
   ("no match","fak_no_match"),("unknown","unknown_remote_state"),("reject","rejection")):
   if marker in text:return name
  return "other_error"
 def dbsample(self):
  query_started=time.monotonic();self.state,self.risk=self.db.risk();risk_ms=(time.monotonic()-query_started)*1000
  if self.state.get("reconciliation_readiness")=="NOT_READY":self.incident("CRITICAL","RECONCILIATION","RECONCILIATION_NOT_READY",evidence=self.state)
  if self.risk["open_quarantines"]:self.incident("CRITICAL","RISK","QUARANTINE",evidence=self.risk)
  if self.risk["managed_dust"]:self.incident("CRITICAL","RISK","OPEN_DUST_BLOCKER",evidence=self.risk)
  incremental_started=time.monotonic();b=self.db.incremental(self.cursors,self.a.incremental_limit);incremental_ms=(time.monotonic()-incremental_started)*1000
  total_ms=risk_ms+incremental_ms;rows_read=sum(len(v) for v in b.values() if isinstance(v,list))
  self.out["db_latency.csv"].write({"timestamp_utc":iso(),"risk_query_ms":round(risk_ms,3),"incremental_query_ms":round(incremental_ms,3),"total_query_ms":round(total_ms,3),"rows_read":rows_read})
  self.metrics.add("sqlite_query_latency_ms",total_ms)
  for r in b["live_reconciliation_runs"]:
   if not r.get("finished_at"):continue
   a,z=epoch(r.get("started_at")),epoch(r.get("finished_at"));duration=(z-a)*1000 if a is not None and z is not None else None
   try:gaps=json.loads(r.get("gaps_json") or "[]")
   except ValueError:gaps=[]
   types=sorted(str(x.get("type") or "unknown") for x in gaps if isinstance(x,dict));fp=hashlib.sha256("|".join(types).encode()).hexdigest()[:16] if types else ""
   self.repeats[fp]+=1;row={"run_id":r.get("id"),"started_at":r.get("started_at"),"finished_at":r.get("finished_at"),
    "duration_ms":duration,"status":r.get("status"),"gap_count":r.get("gaps_count"),"gap_types":"|".join(types),
    "error":r.get("error"),"readiness_after":self.state.get("reconciliation_readiness"),
    "live_blocked":self.state.get("live_blocked_by_reconciliation"),"fingerprint":fp,"repeat_count":self.repeats[fp]}
   self.out["reconciliation.csv"].write(row);self.metrics.c["reconciliation_runs"]+=1;self.metrics.add("reconciliation_duration_ms",duration)
   if r.get("status")=="gaps":self.metrics.c["reconciliation_gaps"]+=1;self.incident("CRITICAL","RECONCILIATION","RECONCILIATION_GAP",evidence=row)
   if r.get("status")=="failed":self.metrics.c["reconciliation_failures"]+=1;self.incident("CRITICAL","RECONCILIATION","RECONCILIATION_FAILED",evidence=row)
   if fp and self.repeats[fp]>=self.a.repeated_gap_count:self.incident("CRITICAL","RECONCILIATION","REPEATED_RECONCILIATION_GAP",evidence=row)
  for r in b["live_order_attempts"]:
   a,z=epoch(r.get("created_at")),epoch(r.get("completed_at"));lat=(z-a)*1000 if a and z else None;ec=self.error_class(r)
   self.out["orders.csv"].write({"rowid":r.get("_cursor"),"occurred_at":r.get("occurred_at"),"attempt_id":r.get("attempt_id"),
    "phase":r.get("phase"),"operation":r.get("operation"),"purpose":r.get("purpose"),"event_id":r.get("event_id"),
    "position_id":r.get("position_id"),"intent_id":r.get("intent_id"),"side":r.get("side"),"order_type":r.get("order_type"),
    "requested_price":r.get("requested_price_text"),"requested_size":r.get("requested_size_text"),
    "requested_amount":r.get("requested_amount_text"),"result_status":r.get("result_status"),"success":r.get("success"),
    "remote_order_id":r.get("remote_order_id"),"error_class":ec,"error_code":r.get("error_code"),
    "http_status":r.get("http_status"),"adapter_latency_ms":lat})
   self.metrics.c["order_"+ec]+=1;self.metrics.add("sell_submit_to_result_ms",lat)
   if r.get("phase")=="RESULT":
    node="BUY_RESULT" if r.get("side")=="BUY" else "SELL_RESULT";self.timeline.add(node,timestamp=r.get("completed_at") or r.get("occurred_at"),
     event_id=r.get("event_id"),position_id=r.get("position_id"),intent_id=r.get("intent_id"),side=r.get("side"),
     price=r.get("requested_price_text"),shares=r.get("requested_size_text"),state=r.get("result_status"),
     reason=ec,source="live_order_attempts",classification="CLEAN" if ec=="success" else "WARNING")
    if ec=="timeout":self.incident("HIGH","ORDER","ORDER_TIMEOUT",evidence=r,intent_id=r.get("intent_id"))
    elif ec not in {"success","fak_no_match"}:self.incident("HIGH","ORDER","ORDER_REJECTED",evidence=r,intent_id=r.get("intent_id"))
  for r in b["live_strategy_entry_audit"]:
   signal=r.get("signal_observed_at");submitted=r.get("submitted_at")
   self.timeline.add("SIGNAL",timestamp=signal or r.get("created_at"),event_id=r.get("event_id"),intent_id=r.get("intent_id"),side=r.get("side"),price=r.get("signal_price_text"),state="OBSERVED",reason="ENTRY_SIGNAL",source="live_strategy_entry_audit",classification="CLEAN")
   self.timeline.add("REVALIDATION",timestamp=submitted or r.get("updated_at"),event_id=r.get("event_id"),intent_id=r.get("intent_id"),side=r.get("side"),price=r.get("revalidation_ask_text"),state=r.get("revalidation_result"),reason=r.get("entry_validity"),source="live_strategy_entry_audit",classification="CLEAN" if str(r.get("revalidation_result") or "").upper() in {"OK","PASS","ACCEPTED"} else "WARNING")
   a,z=epoch(signal),epoch(submitted);self.metrics.add("signal_to_submit_ms",(z-a)*1000 if a is not None and z is not None else None)
   self.metrics.add("signal_to_fill_ms",r.get("signal_to_fill_ms"))
   if str(r.get("entry_validity") or "").upper()=="INVALID":self.metrics.c["invalid_entries"]+=1;self.incident("CRITICAL","ENTRY","ENTRY_FILL_OUTSIDE_POLICY",evidence=r,event_id=r.get("event_id"),intent_id=r.get("intent_id"))
  for r in b["live_strategy_intents"]:
   node="ENTRY_INTENT" if r.get("action")=="ENTRY" else ("TP_INTENT" if r.get("action")=="TP" else "EXIT_INTENT")
   self.timeline.add(node,timestamp=r.get("created_at"),event_id=r.get("event_id"),position_id=r.get("position_id"),
    intent_id=r.get("intent_id"),side=r.get("side"),price=r.get("price_limit_text"),shares=r.get("requested_shares_text"),
    state=r.get("state"),reason=r.get("reason_code"),source="live_strategy_intents",classification="CLEAN")
   if r.get("action")=="ENTRY":self.metrics.c["entries"]+=1
   reason=str(r.get("reason_code") or "")
   if r.get("state")=="ZERO_FILL":self.metrics.c["zero_fills"]+=1;self.incident("WARNING","ORDER","ZERO_FILL",evidence=r,intent_id=r.get("intent_id"))
   if reason.startswith("ENTRY_REVALIDATION_") or reason=="ENTRY_SIGNAL_EXPIRED":self.incident("WARNING","ENTRY","ENTRY_REVALIDATION_FAILED",evidence=r,intent_id=r.get("intent_id"))
   if reason=="ENTRY_FILL_OUTSIDE_POLICY":self.metrics.c["invalid_entries"]+=1;self.incident("CRITICAL","ENTRY","ENTRY_FILL_OUTSIDE_POLICY",evidence=r,intent_id=r.get("intent_id"))
  for r in b["live_strategy_fills"]:
   self.metrics.c["fills"]+=1
   action=str(r.get("action") or "").upper();side=str(r.get("side") or "").upper();purpose=str(r.get("purpose") or "").upper()
   if side=="BUY":self.metrics.c["buy_fills"]+=1
   if side=="SELL":self.metrics.c["sell_fills"]+=1
   if action=="TP" or purpose=="TAKE_PROFIT":self.metrics.c["tp_fills"]+=1
   if str(r.get("status") or "").upper().startswith("PARTIAL"):self.metrics.c["partial_fills"]+=1
   self.timeline.add("FILL",timestamp=r.get("matched_at") or r.get("created_at"),
    event_id=r.get("event_id"),position_id=r.get("position_id"),intent_id=r.get("intent_id"),price=r.get("price_text"),shares=r.get("shares_text"),state=r.get("status"),
    reason=r.get("fee_verification_status"),source="live_strategy_fills",classification="CLEAN")
   if r.get("fee_verification_status")=="VERIFIED" and number(r.get("fee_text"),0)==0 and str(r.get("fee_source") or "").lower().startswith("taker"):
    self.incident("CRITICAL","ACCOUNTING","FEE_ACCOUNTING_INVALID",evidence=r,intent_id=r.get("intent_id"))
  for r in b["live_audit_timeline"]:
   try:p=json.loads(r.get("parameters_json") or "{}")
   except ValueError:p={}
   node={"ENTRY_SIGNAL":"SIGNAL","ENTRY_REVALIDATION":"REVALIDATION","STOP_LATCHED":"STOP_LATCH",
    "STOP_TRIGGER":"STOP_CONDITION","POSITION_HOT":"POSITION_HOT","EXIT_EVALUATION":"FIRST_EXIT_EVAL"}.get(r.get("reason_code"),r.get("reason_code") or r.get("category"))
   if str(r.get("reason_code") or "") in {"STOP_TRIGGER","STOP_LATCHED","STOP_CONDITION"}:self.metrics.c["stop_triggers"]+=1
   self.timeline.add(node,timestamp=r.get("occurred_at"),event_id=r.get("event_id"),intent_id=r.get("intent_id"),
    side=r.get("side"),price=r.get("average_price_text"),shares=r.get("filled_shares_text"),state=r.get("new_state"),
    reason=r.get("reason_code"),source="live_audit_timeline",classification="INCIDENT" if r.get("severity") in {"HIGH","CRITICAL"} else "CLEAN")
   audit_incidents={
    "ENTRY_SIGNAL_EXPIRED":("WARNING","ENTRY"),"ENTRY_FILL_OUTSIDE_POLICY":("CRITICAL","ENTRY"),
    "POSITION_PUBLICATION_SLOW":("HIGH","POSITION"),"POSITION_EXIT_EVALUATION_SLOW":("HIGH","POSITION"),
    "STOP_LATCH_SLOW":("HIGH","STOP"),"STOP_SELL_SUBMISSION_SLOW":("CRITICAL","STOP"),
    "STOP_NO_SELL_ATTEMPT":("CRITICAL","STOP"),"ORDER_UNKNOWN":("CRITICAL","ORDER"),
    "BOOK_INTEGRITY_FAILURE":("HIGH","MARKET_WS"),"EXIT_SUPERVISOR_SLA_EXCEEDED":("HIGH","EXIT_SUPERVISOR"),
    "EXIT_SUPERVISOR_STARVATION":("CRITICAL","EXIT_SUPERVISOR"),"RESOLUTION_CONTRADICTION":("CRITICAL","RESOLUTION")}
   code=str(r.get("reason_code") or "").upper()
   if code in audit_incidents:
    sev,cat=audit_incidents[code];self.incident(sev,cat,code,evidence=r,event_id=r.get("event_id"),intent_id=r.get("intent_id"))
   for k in ("signal_to_revalidation_ms","signal_to_submit_ms","signal_to_fill_ms","fill_to_position_ms",
    "fill_to_hot_state_ms","hot_state_to_first_exit_eval_ms","stop_condition_to_latch_ms",
    "stop_latch_to_sell_submit_ms","stop_condition_to_sell_submit_ms","sell_submit_to_result_ms","sell_submit_to_fill_ms"):self.metrics.add(k,p.get(k))
  for r in b["live_position_events"]:self.timeline.add(r.get("event_type"),timestamp=r.get("occurred_at"),event_id=r.get("event_id"),
   position_id=r.get("position_id"),shares=r.get("shares_text"),state=r.get("new_state"),reason=r.get("previous_state"),
   source="live_position_events",classification="CLEAN")
  for r in b["live_strategy_deals"]:
   pnl=number(r.get("realized_pnl_text"));self.metrics.add("realized_pnl",pnl);self.metrics.add("fees",r.get("total_fees_text"))
   if pnl is not None:self.metrics.c["winners" if pnl>0 else "losers" if pnl<0 else "breakeven"]+=1
   self.timeline.add("DEAL_FINAL",timestamp=r.get("closed_at") or r.get("updated_at"),event_id=r.get("event_id"),position_id=r.get("position_id"),state=r.get("state"),reason=r.get("final_reason"),source="live_strategy_deals",classification="CLEAN")
  for r in b["positions"]:
   terminal=r.get("state")=="DUST" and (r.get("closed_at") or number(r.get("sellable_shares_text"),0)==0)
   classification="TERMINAL_DUST" if terminal else ("ACTIVE_RISK" if r.get("state") in ACTIVE else "MANAGED_DUST")
   self.out["positions.csv"].write({"position_id":r.get("position_id"),"event_id":r.get("event_id"),"token_id":r.get("token_id"),
    "state":r.get("state"),"remaining_shares":r.get("remaining_shares_text"),"sellable_shares":r.get("sellable_shares_text"),
    "dust_shares":r.get("dust_shares_text"),"stop_stage":r.get("stop_stage"),"tp_intent_id":r.get("tp_intent_id"),
    "active_exit_intent_id":r.get("active_exit_intent_id"),"resolved_winner":r.get("resolved_winner"),
    "exit_value":r.get("exit_value_text"),"realized_pnl":r.get("realized_pnl_text"),
    "exit_obligation_reason":r.get("exit_obligation_reason"),"updated_at":r.get("updated_at"),"classification":classification})
 def summary(self,state):
  sev=Counter(x["severity"] for x in self.inc.items.values());verdict="FAIL" if sev["CRITICAL"] else ("PASS_WITH_WARNINGS" if sev["HIGH"] or sev["WARNING"] else "PASS")
  latency={k:self.metrics.summary(k) for k in ("signal_to_revalidation_ms","signal_to_submit_ms","signal_to_fill_ms",
  "fill_to_position_ms","fill_to_hot_state_ms","hot_state_to_first_exit_eval_ms","stop_condition_to_latch_ms",
  "stop_latch_to_sell_submit_ms","stop_condition_to_sell_submit_ms","sell_submit_to_result_ms","sell_submit_to_fill_ms")}
  return {"run_id":self.run_id,"start":self.metadata["started_at_utc"],"end":iso(),"duration_seconds":round(time.monotonic()-self.mono,3),
   "samples":self.samples,"missed_sampling_deadlines":self.missed,"monitor_output_bytes":self.budget.written,
   "service_restarts":self.metrics.c["service_restarts"],"service_downtime_seconds":round(self.downtime+(time.monotonic()-self.down_since if self.down_since is not None else 0),3),"trades":self.metrics.c["entries"],
   "entries":self.metrics.c["entries"],"buy_fills":self.metrics.c["buy_fills"],"sell_fills":self.metrics.c["sell_fills"],
   "tp_fills":self.metrics.c["tp_fills"],"stop_triggers":self.metrics.c["stop_triggers"],
   "invalid_entries":self.metrics.c["invalid_entries"],"partial_fills":self.metrics.c["partial_fills"],
   "zero_fills":self.metrics.c["zero_fills"],"reconciliation_runs":self.metrics.c["reconciliation_runs"],
   "reconciliation_gaps":self.metrics.c["reconciliation_gaps"],"reconciliation_failures":self.metrics.c["reconciliation_failures"],
   "reconciliation_duration":self.metrics.summary("reconciliation_duration_ms"),
   "order_error_counts":{k[6:]:v for k,v in self.metrics.c.items() if k.startswith("order_")},
   "market_ws_disconnects":sum(x["code"]=="MARKET_WS_DISCONNECT" for x in self.inc.items.values()),
   "user_ws_disconnects":sum(x["code"]=="USER_WS_DISCONNECT" for x in self.inc.items.values()),
   "incidents_info":sev["INFO"],"incidents_warning":sev["WARNING"],"incidents_high":sev["HIGH"],
   "incidents_critical":sev["CRITICAL"],"latency_metrics":latency,
   "cpu_stats":{"monitor":self.metrics.summary("monitor_cpu_percent"),"trader":self.metrics.summary("trader_cpu_percent")},
   "memory_stats":{"monitor":self.metrics.summary("monitor_rss_kb"),"trader":self.metrics.summary("trader_rss_kb")},
   "event_loop_stats":self.metrics.summary("event_loop_lag_ms"),
   "sample_loop_duration":self.metrics.summary("sample_loop_duration_ms"),"sqlite_query_latency":self.metrics.summary("sqlite_query_latency_ms"),
   "db_growth":self.metrics.summary("db_growth_bytes"),
   "pnl":{"realized":self.metrics.summary("realized_pnl"),"fees":self.metrics.summary("fees"),"winners":self.metrics.c["winners"],"losers":self.metrics.c["losers"]},
   "active_risk":self.risk,"state":state,"verdict":verdict}
 def report(self,s,checkpoint=None):
  verdict=s["verdict"]
  if checkpoint:verdict={"PASS":"PASS_CONTINUE","PASS_WITH_WARNINGS":"PASS_WITH_WARNINGS_CONTINUE","FAIL":"FAIL_REVIEW_RECOMMENDED"}[verdict]
  rows="\n".join(f"| {x['severity']} | {x['first_seen']} | {x['code']} | {x.get('event_id') or '-'} | count={x['occurrence_count']} | נדרשת בדיקת evidence |" for x in sorted(self.inc.items.values(),key=lambda x:-SEV[x["severity"]])[:50]) or "| - | - | אין | - | לא זוהו incidents | - |"
  lat="\n".join(f"- {k}: count={v['count']}, min={v['min']}, p50={v['p50']}, p95={v['p95']}, p99={v['p99']}, max={v['max']}" for k,v in s["latency_metrics"].items())
  title=f"{checkpoint:g}h LIVE Soak Checkpoint" if checkpoint else "72h LIVE Soak Report"
  return f"""# {title}

## 1. תקציר מנהלים (Executive Summary)
ניטור read-only; דגימות: {s['samples']}; incidents ייחודיים: {len(self.inc.items)}; מצב סיום: {s['state']}.

## 2. פסק דין (Verdict)
**{verdict}**

## 3. חלון הבדיקה (Test Window)
UTC: {s['start']} עד {s['end']}

ישראל: {israel(s['start'])} עד {israel(s['end'])}

## 4. גרסת Runtime
SHA: {self.metadata['git_sha']}; branch: {self.metadata['branch']}; dirty: {self.metadata['git_dirty']}; mode: {self.metadata.get('mode')}; config hash: {self.metadata.get('config_hash')}.

## 5. פעילות מסחר
Entries: {s['entries']}; BUY fills: {s['buy_fills']}; SELL fills: {s['sell_fills']}; TP fills: {s['tp_fills']}; STOP: {s['stop_triggers']}; partial: {s['partial_fills']}; zero fills: {s['zero_fills']}; PnL: {s['pnl']}.

## 6. איכות כניסה
כניסות לא תקינות: {s['invalid_entries']}. פירוט signal/revalidation/execution נמצא ב-trades.csv וב-trade_timelines.jsonl.

## 7. ניהול פוזיציות
סיכון פעיל בסיום: {json.dumps(s['active_risk'],sort_keys=True)}.

## 8. Stop Loss
מספר triggers: {s['stop_triggers']}; זמני latch/submission/result מסוכמים להלן.

## 9. Exit Supervisor
מצב, ריצות, SLA, concurrency, deferrals, pending evaluations ו-REST failures נשמרים ב-runtime.csv.

## 10. Orders / CLOB
סיווגי תוצאה: {json.dumps(s['order_error_counts'],sort_keys=True)}. פירוט ב-orders.csv.

## 11. Market WS
Disconnect incidents: {s['market_ws_disconnects']}; מגמות readiness/staleness/queues ב-market_ws.csv.

## 12. User WS
Disconnect incidents: {s['user_ws_disconnects']}; פירוט ב-user_ws.csv.

## 13. Reconciliation
ריצות: {s['reconciliation_runs']}; gaps: {s['reconciliation_gaps']}; failures: {s['reconciliation_failures']}; duration: {s['reconciliation_duration']}.

## 14. DUST / Residue
DUST היסטורי סגור מסווג TERMINAL_DUST ואינו נספר כחשיפה פעילה.

## 15. Accounting / Fees
חריגות fee מתועדות כ-FEE_ACCOUNTING_INVALID; סיכום: {s['pnl']['fees']}.

## 16. שימוש במשאבים
CPU: {s['cpu_stats']}; memory: {s['memory_stats']}; DB growth: {s['db_growth']}; sample loop: {s['sample_loop_duration']}; SQLite: {s['sqlite_query_latency']}; downtime seconds: {s['service_downtime_seconds']}.

## 17. Incidents
| Severity | Time | Code | Event | Description | Resolution |
|---|---|---|---|---|---|
{rows}

## 18. סיכום Latency
{lat}

## 19. עשר החריגות המרכזיות
האירועים החמורים ביותר מופיעים בטבלה וב-incident_snapshots/.

## 20. המלצות
יש לבדוק evidence של HIGH/CRITICAL לפני החלטת deployment. המוניטור אינו משנה את הרובוט.

## 21. סיכונים נותרים
ערך NULL משמעו שטלמטריה לא הייתה זמינה; הוא אינו הוכחת תקינות.

## 22. פסק דין סופי
**{verdict}**
"""
 def checkpoint(self,h):
  s=self.summary("RUNNING");name=f"{int(h):02d}h.md" if h.is_integer() else f"{h:g}h.md"
  (self.dir/"checkpoints"/name).write_text(self.report(s,h));self.checkpoints.append(h);self.log.log("INFO",f"checkpoint {h:g}h")
 def status(self,state):
  elapsed=time.monotonic()-self.mono;atomic(self.dir/"status.json",{"run_id":self.run_id,"run_dir":str(self.dir),
   "monitor_pid":os.getpid(),"state":state,"elapsed_seconds":round(elapsed,3),
   "remaining_seconds":round(max(0,self.a.duration_seconds-elapsed),3),"samples":self.samples,
   "trades":self.metrics.c["entries"],"incidents":len(self.inc.items),
   "latest_checkpoint":self.checkpoints[-1] if self.checkpoints else None,"output_bytes":self.budget.written,"updated_at":iso()})
 def run(self):
  self.log.log("INFO",f"start run={self.run_id} duration={self.a.duration_seconds}");n=time.monotonic();nr=n;nd=n;state="RUNNING"
  self.poll_thread=threading.Thread(target=self.poll_status,name="soak-status-poller",daemon=True);self.poll_thread.start()
  try:
   while True:
    tm=time.monotonic();elapsed=tm-self.mono
    if _STOP:state="STOPPED_EARLY";self.stop_reason="SIGNAL";break
    if elapsed>=self.a.duration_seconds:
     for h in self.a.checkpoints:
      if h not in self.checkpoints and h*3600<=self.a.duration_seconds:self.checkpoint(h)
     state="COMPLETE";break
    if self.a.once and self.samples:state="STOPPED_EARLY";self.stop_reason="ONCE";break
    if tm>=n:self.sample(n);n+=self.a.sample_interval_seconds
    if tm>=nr:self.resources();nr=tm+self.a.resource_interval_seconds
    if tm>=nd:self.dbsample();nd=tm+self.a.db_interval_seconds
    for h in self.a.checkpoints:
     if h not in self.checkpoints and elapsed>=h*3600:self.checkpoint(h)
    self.status(state)
    if self.budget.reason:state="STOPPED_EARLY";self.stop_reason=self.budget.reason;break
    time.sleep(min(.25,max(.01,n-time.monotonic())))
  except OSError as e:state="STOPPED_EARLY";self.stop_reason="OUTPUT_GUARD:"+str(e)
  finally:
   self.poll_stop.set()
   if self.poll_thread:self.poll_thread.join(timeout=min(2.0,self.a.ipc_timeout))
   for fp,x in self.captures.items():atomic(self.dir/"incident_snapshots"/(fp+".json"),x["data"])
   s=self.summary(state);s["stop_reason"]=self.stop_reason;atomic(self.dir/"summary.json",s)
   (self.dir/"final_report.md").write_text(self.report(s));self.metadata.update({"status":state,"ended_at_utc":iso(),"stop_reason":self.stop_reason})
   atomic(self.dir/"metadata.json",self.metadata);self.status(state);self.log.log("INFO",f"exit {state} {self.stop_reason}")
  return 0

def latest(out):
 p=Path(out)/"latest_run.txt"
 if not p.exists():raise SystemExit("No latest run")
 return Path(p.read_text().strip())
def show_status(out):
 p=latest(out)/"status.json";data=json.loads(p.read_text());pid=int(data.get("monitor_pid") or 0)
 try:os.kill(pid,0);alive=pid>0
 except OSError:alive=False
 data["process_alive"]=alive;print(json.dumps(data,indent=2,sort_keys=True));return 0
def parse(argv=None):
 p=argparse.ArgumentParser(description="Read-only 72h Polymarket LIVE soak monitor")
 p.add_argument("--output-dir",default="/opt/polymarket-btc-live/output/soak");p.add_argument("--run-dir");p.add_argument("--run-id");p.add_argument("--resume")
 p.add_argument("--duration-hours",type=float,default=72);p.add_argument("--duration-seconds",type=float)
 p.add_argument("--sample-interval-seconds",type=float,default=5);p.add_argument("--sample-interval",type=float)
 p.add_argument("--resource-interval-seconds",type=float,default=30);p.add_argument("--resource-interval",type=float)
 p.add_argument("--db-interval-seconds",type=float,default=15);p.add_argument("--db-interval",type=float)
 p.add_argument("--checkpoints",default="2,6,24,48,72");p.add_argument("--db-path",default="/opt/polymarket-btc-live/poly_live.sqlite3")
 p.add_argument("--socket-path",default="/run/polymarket/trader.sock");p.add_argument("--trader-service",default="polymarket-trader.service")
 p.add_argument("--git-root",default="/opt/polymarket-btc-live/repo");p.add_argument("--ipc-timeout",type=float,default=5);p.add_argument("--status-interval-seconds",type=float,default=30)
 p.add_argument("--incremental-limit",type=int,default=2000);p.add_argument("--ring-buffer-seconds",type=float,default=120)
 p.add_argument("--post-incident-seconds",type=float,default=120);p.add_argument("--max-output-bytes",type=int,default=2_000_000_000)
 p.add_argument("--min-free-bytes",type=int,default=2_000_000_000);p.add_argument("--disk-low-bytes",type=int,default=4_000_000_000)
 p.add_argument("--db-growth-high-bytes",type=int,default=5_000_000_000);p.add_argument("--cpu-high-percent",type=float,default=90)
 p.add_argument("--memory-high-mb",type=float,default=2048);p.add_argument("--event-loop-lag-high-ms",type=float,default=1000);p.add_argument("--exit-supervisor-sla-ms",type=float,default=5000)
 p.add_argument("--repeated-gap-count",type=int,default=3);p.add_argument("--ws-backlog-ratio",type=float,default=.8);p.add_argument("--once",action="store_true");p.add_argument("--check-config",action="store_true");p.add_argument("--status",action="store_true")
 a=p.parse_args(argv);a.duration_seconds=a.duration_seconds if a.duration_seconds is not None else a.duration_hours*3600
 if a.sample_interval is not None:a.sample_interval_seconds=a.sample_interval
 if a.resource_interval is not None:a.resource_interval_seconds=a.resource_interval
 if a.db_interval is not None:a.db_interval_seconds=a.db_interval
 a.checkpoints=[float(x) for x in a.checkpoints.split(",") if x.strip()]
 return a
def check(a):
 db=DB(a.db_path)
 with db.connect() as c:q=int(c.execute("PRAGMA query_only").fetchone()[0]);c.execute("SELECT 1")
 st=IPC(a.socket_path,a.ipc_timeout).status();print(json.dumps({"ok":True,"db_query_only":q==1,"ipc_command":"STATUS",
 "mode":nested(st,"strategy","mode"),"service":service(a.trader_service)},indent=2));return 0
def main(argv=None):
 a=parse(argv)
 if a.status:return show_status(a.output_dir)
 if a.check_config:return check(a)
 return Monitor(a).run()
if __name__=="__main__":raise SystemExit(main())
