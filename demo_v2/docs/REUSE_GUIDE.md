# DEMO V2 — Reuse Guide

## REUSE
Use existing code with minimal changes when behavior matches current spec, tests exist, and it can run without coupling DEMO to real-trading credentials/submission. Likely candidates after verification: public Market WS transport, order-book parsing/state, market discovery/rollover helpers, official resolution parsing, decimal normalization, freshness/telemetry, retry/backoff, generic SQLite/repository patterns, safe auth primitives, fee/accounting helpers.

## ADAPT
Likely: existing Paper Trading, legacy DEMO rule/deal engine, LIVE strategy persistence/state patterns, pause/recovery/readiness concepts, dashboard API/auth structure. Adapt for 21 markets, shared frequency, concurrent positions, trigger re-arm, observed-price TP/SL, schedules/internal windows, underlying filters, global/per-rule wallets, richer accounting, no backfill and dynamic listening.

## REFERENCE ONLY
Archived reports, historical DEMO semantics, old monolithic paths, one-open-deal restrictions, side-specific caps, configured-threshold exit storage, older fixed-fee assumptions.

## DO NOT USE IN DEMO EXECUTION
Real order submission, authenticated trading write methods, signing/private keys, allowance setup, redemption, real cancellation, emergency close, LIVE canary/arming, mutation of LIVE positions/intents/fills/system state, LIVE DB as DEMO DB.

Before broad implementation create a reuse map with capability, existing source, classification (REUSE/ADAPT/REFERENCE/DO NOT USE), planned action and reason. Do not rewrite merely because existing code is complex.
