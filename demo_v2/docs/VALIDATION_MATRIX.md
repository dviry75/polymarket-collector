# DEMO V2 — Validation Matrix

Entry: 0.75→0.74 with trigger 0.74 = one entry; 0.75→0.73 = none; repeated 0.74 = no retrigger; 0.74→0.73→0.74 = new trigger if frequency remains.

Frequency: default 1; frequency 2 allows two entries while first can remain open; shared UP/DOWN; resets next market instance.

Schedule: Asia/Jerusalem, multiple ranges, cross-midnight, none=24/7; schedule off blocks new entries only.

Internal windows: correct 5m/1h relative timing; none=full market; requires both general + internal eligibility.

Lifecycle: edit affects new positions only; disable/delete stops entries only; open positions keep snapshot; historical attribution preserved.

TP/SL: SL 0.66 with 0.68→0.63 exits 0.63; TP 0.96 with 0.94→0.97 exits 0.97; no artificial threshold fill.

Resolution: winner 1.00; loser 0.00; no fake sell.

Disconnect: missed trigger/TP/SL during gap not backfilled; first fresh snapshot can trigger; verified resolution can close existing position.

Dynamic listening: no eligible rule + no open position => no listening required; eligibility resumes => subscribe; open position keeps monitoring.

Underlying filter: percentage/absolute movement; >/< /between; direction modes; stale required data blocks entry; observations/provenance persisted.

Fees/accounting: min 5 shares; gross vs net shares preserved; buy/sell fees correct; resolution does not invent sell fee; global wallet starts zero and may go negative; per-rule ledger correct; P&L reconciles.

KPI: positive/negative based on closed net P&L; break-even excluded; filters align table/KPI; total fees reconcile.

Health: socket open but stale data != READY; missing fee data blocks entries; stale required underlying blocks affected entries; reconnect/re-subscribe; return READY only when healthy; no backfill.

Isolation: separate DEMO DB/config/service; no path to real adapter write methods; tests never source prod trading env; reset cannot affect LIVE.
