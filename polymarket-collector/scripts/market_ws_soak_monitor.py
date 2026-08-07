#!/usr/bin/env python3
"""Autonomous, read-only Market WebSocket soak monitor."""
from __future__ import annotations
from array import array
import argparse, json, os, sqlite3, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

METRICS=("exchange_to_socket_receive_ms","socket_receive_to_handler_ms","handler_to_book_update_ms","book_update_to_strategy_ms","total_processing_ms","event_loop_lag_ms","ws_internal_queue_depth","tcp_recv_q_bytes","recv_wait_ms","between_recv_gap_ms","parse_ms","ingress_queue_wait_ms","ingress_queue_depth")
TABLES=("live_orders","live_order_fills","live_strategy_intents","live_strategy_fills")
EXPECTED={"kill_switch":"true","pause_entries":"true","canary_armed":"false"}
def now(): return datetime.now(timezone.utc).isoformat()
def israel(): return datetime.now(ZoneInfo("Asia/Jerusalem")).isoformat()
def service():
 r=subprocess.run(["systemctl","show","polymarket-live.service","-p","ActiveState","-p","MainPID"],capture_output=True,text=True)
 return dict(line.split("=",1) for line in r.stdout.splitlines() if "=" in line)
def db(path):
 c=sqlite3.connect(f"file:{path}?mode=ro",uri=True,timeout=5); c.row_factory=sqlite3.Row
 try:
  states={r["key"]:str(r["value"]) for r in c.execute("SELECT key,value FROM live_system_state WHERE key IN ('kill_switch','pause_entries','canary_armed','strategy_readiness','strategy_block_reason','market_ws_status')")}
  counts={t:int(c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]) for t in TABLES}
  markets=[dict(r) for r in c.execute("SELECT id,event_id,condition_id,yes_token_id,no_token_id,created_at FROM live_markets WHERE market_resolved=0 AND accepting_orders=1 ORDER BY id DESC LIMIT 2")]
  return {"states":states,"counts":counts,"markets":markets}
 finally: c.close()
def proc(pid,prev):
 p=Path(f"/proc/{pid}/stat")
 if not p.exists(): return {"alive":False},prev
 s=p.read_text().split(); ticks=os.sysconf(os.sysconf_names["SC_CLK_TCK"]); cpu=(int(s[13])+int(s[14]))/ticks; cur=(time.monotonic(),cpu)
 pct=None if prev is None else (cpu-prev[1])*100/max(.001,cur[0]-prev[0])
 st={}
 for line in Path(f"/proc/{pid}/status").read_text().splitlines():
  if line.startswith(("VmRSS:","VmHWM:","Threads:")): k,v=line.split(":",1); st[k]=v.strip()
 return {"alive":True,"cpu_percent":pct,"cpu_seconds":cpu,"rss":st.get("VmRSS"),"rss_hwm":st.get("VmHWM"),"threads":st.get("Threads")},cur
def summary(a):
 if not a:return {"count":0,"p50":None,"p95":None,"p99":None,"max":None}
 v=sorted(a)
 def q(x):return round(float(v[min(len(v)-1,max(0,int(len(v)*x)-1))]),4)
 return {"count":len(v),"p50":q(.5),"p95":q(.95),"p99":q(.99),"max":round(float(v[-1]),4)}
def main():
 ap=argparse.ArgumentParser(); ap.add_argument("--duration",type=int,default=7200); ap.add_argument("--interval",type=float,default=5); ap.add_argument("--pid",type=int,required=True); ap.add_argument("--db",type=Path,default=Path("/opt/polymarket-btc-live/poly_live.sqlite3")); ap.add_argument("--diagnostics",type=Path,default=Path("/opt/polymarket-btc-live/output/market_ws_latency_diagnostics.json")); ap.add_argument("--output",type=Path,required=True); a=ap.parse_args()
 start=time.monotonic(); start_ms=int(time.time()*1000); started_utc=now(); started_israel=israel(); initial=db(a.db); initial_service=service(); deadline=start+a.duration
 allv={m:array("d") for m in METRICS}; wins=[{m:array("d") for m in METRICS} for _ in range((a.duration+899)//900)]
 samples=[]; rotations=[]; conns=[]; ready=[]; safety=[]; count_bad=[]; laststamp=None; lastkey=None; lastgen=None; latest_health={}; diag_count=0; prevproc=None
 lasttokens=tuple(x for m in initial["markets"] for x in (str(m["yes_token_id"]),str(m["no_token_id"]))); lastready=(initial["states"].get("strategy_readiness"),initial["states"].get("strategy_block_reason")); nxt=start
 while time.monotonic()<deadline:
  if time.monotonic()<nxt: time.sleep(min(.5,nxt-time.monotonic())); continue
  elapsed=time.monotonic()-start; nxt+=a.interval; snap=db(a.db); svc=service(); ps,prevproc=proc(a.pid,prevproc); fs=os.statvfs(a.output.parent)
  for k,e in EXPECTED.items():
   if snap["states"].get(k)!=e:safety.append({"at":now(),"key":k,"expected":e,"actual":snap["states"].get(k)})
  for t,n in initial["counts"].items():
   if snap["counts"][t]!=n:count_bad.append({"at":now(),"table":t,"initial":n,"current":snap["counts"][t]})
  rr=(snap["states"].get("strategy_readiness"),snap["states"].get("strategy_block_reason"))
  if rr!=lastready:ready.append({"at":now(),"elapsed_seconds":elapsed,"from":lastready,"to":rr});lastready=rr
  tokens=tuple(x for m in snap["markets"] for x in (str(m["yes_token_id"]),str(m["no_token_id"])))
  if tokens!=lasttokens:rotations.append({"at":now(),"elapsed_seconds":elapsed,"old_tokens":lasttokens,"new_tokens":tokens,"markets":snap["markets"],"generation":lastgen});lasttokens=tokens
  if a.diagnostics.exists():
   try:d=json.loads(a.diagnostics.read_text())
   except Exception:d={}
   stamp=d.get("generated_at")
   if stamp and stamp!=laststamp:
    laststamp=stamp;diag_count+=1;latest_health=d.get("health") or {};gen=(d.get("connection") or {}).get("generation")
    if lastgen is not None and gen!=lastgen:conns.append({"at":now(),"elapsed_seconds":elapsed,"from":lastgen,"to":gen,"connection":d.get("connection")})
    lastgen=gen
    for r in d.get("recent_records") or []:
     wall=r.get("socket_receive_wall_ms")
     if wall is None or int(wall)<start_ms:continue
     key=(int(r.get("connection_generation") or 0),float(r.get("recv_return_monotonic") or 0),int(r.get("batch_index") or 0))
     if lastkey is not None and key<=lastkey:continue
     wi=min(len(wins)-1,max(0,int((int(wall)-start_ms)/900000)))
     for m in METRICS:
      if r.get(m) is not None:allv[m].append(float(r[m]));wins[wi][m].append(float(r[m]))
     lastkey=key
  samples.append({"at":now(),"elapsed_seconds":round(elapsed,3),"service":svc,"process":ps,"disk_free_bytes":fs.f_bavail*fs.f_frsize,"states":snap["states"],"counts":snap["counts"],"generation":lastgen,"health":latest_health})
  checkpoint={"status":"RUNNING","started_utc":started_utc,"started_israel":started_israel,"elapsed_seconds":elapsed,"target_seconds":a.duration,"pid":a.pid,"initial_db":initial,"initial_service":initial_service,"samples":samples,"rotations":rotations,"connection_changes":conns,"readiness_changes":ready,"safety_violations":safety,"count_violations":count_bad,"diagnostic_snapshots":diag_count,"captured_record_count":len(allv["total_processing_ms"])}
  tmp=a.output.with_suffix(".tmp");tmp.write_text(json.dumps(checkpoint,sort_keys=True,default=str));tmp.replace(a.output)
 final=db(a.db); result={"status":"COMPLETE","started_utc":started_utc,"ended_utc":now(),"started_israel":started_israel,"ended_israel":israel(),"duration_seconds":time.monotonic()-start,"target_seconds":a.duration,"pid":a.pid,"initial_db":initial,"final_db":final,"initial_service":initial_service,"final_service":service(),"metrics":{m:summary(v) for m,v in allv.items()},"windows":[{"index":i+1,"start_offset_seconds":i*900,"end_offset_seconds":min(a.duration,(i+1)*900),"metrics":{m:summary(v) for m,v in w.items()}} for i,w in enumerate(wins)],"samples":samples,"rotations":rotations,"connection_changes":conns,"readiness_changes":ready,"safety_violations":safety,"count_violations":count_bad,"diagnostic_snapshots":diag_count,"captured_record_count":len(allv["total_processing_ms"]),"latest_health":latest_health}
 tmp=a.output.with_suffix(".tmp");tmp.write_text(json.dumps(result,sort_keys=True,default=str));tmp.replace(a.output);print(json.dumps({"status":"COMPLETE","output":str(a.output),"duration_seconds":result["duration_seconds"],"records":result["captured_record_count"],"rotations":len(rotations),"connections":len(conns),"safety_violations":len(safety),"count_violations":len(count_bad)}),flush=True)
if __name__=="__main__":main()
