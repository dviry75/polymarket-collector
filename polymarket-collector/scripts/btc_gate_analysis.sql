-- BTC reference gate: analysis after the observe period.
-- Read-only. Run against a COPY of the live DB, e.g.:
--   sqlite3 -readonly /path/to/copy.sqlite3 < scripts/btc_gate_analysis.sql
--
-- Bucket bounds (directional delta = BTC move toward the traded side, USD):
--   '<0'     : delta < 0
--   '0-10'   : 0 <= delta < 10
--   '10-25'  : 10 <= delta < 25   (the default gate passes from exactly 10)
--   '25-50'  : 25 <= delta <= 50
--   '>50'    : delta > 50
--   'NULL'   : no BTC value (feed unavailable / stale / open unknown)

.headers on
.mode column

-- (a) Real deals: win rate and PnL by directional delta at entry.
-- A win is realized PnL > 0. Deals still open, or holding a winner that is
-- not yet redeemed (REDEEM_PENDING), are listed separately in pending_*.
WITH deals AS (
    SELECT
        CAST(btc_entry_directional_delta_usd AS REAL) AS delta,
        CAST(realized_pnl_text AS REAL) AS pnl,
        state,
        btc_gate_reason,
        btc_exit_type
    FROM live_strategy_deals
    WHERE entry_intent_id IS NOT NULL
      AND state IN ('CLOSED','DUST','RESOLVED_LOSER','RESOLVED_WINNER','REDEEM_PENDING')
      AND btc_gate_mode IS NOT NULL
),
bucketed AS (
    SELECT *,
        CASE
            WHEN delta IS NULL THEN 'NULL'
            WHEN delta < 0 THEN '<0'
            WHEN delta < 10 THEN '0-10'
            WHEN delta < 25 THEN '10-25'
            WHEN delta <= 50 THEN '25-50'
            ELSE '>50'
        END AS bucket
    FROM deals
)
SELECT
    bucket,
    COUNT(*) AS deals,
    SUM(state <> 'REDEEM_PENDING') AS settled,
    SUM(state = 'REDEEM_PENDING') AS pending_redeem,
    SUM(state <> 'REDEEM_PENDING' AND pnl > 0) AS wins,
    SUM(state <> 'REDEEM_PENDING' AND pnl < 0) AS losses,
    ROUND(100.0 * SUM(state <> 'REDEEM_PENDING' AND pnl > 0)
          / NULLIF(SUM(state <> 'REDEEM_PENDING' AND pnl <> 0), 0), 1) AS win_rate_pct,
    ROUND(SUM(CASE WHEN state <> 'REDEEM_PENDING' THEN pnl END), 4) AS pnl_usd,
    ROUND(AVG(CASE WHEN state <> 'REDEEM_PENDING' THEN pnl END), 4) AS avg_pnl_usd,
    SUM(btc_exit_type = 'TP') AS exit_tp,
    SUM(btc_exit_type = 'STOP') AS exit_stop,
    SUM(btc_exit_type = 'EMERGENCY') AS exit_emergency,
    SUM(btc_exit_type = 'RESOLUTION') AS exit_resolution
FROM bucketed
GROUP BY bucket
ORDER BY CASE bucket WHEN '<0' THEN 0 WHEN '0-10' THEN 1 WHEN '10-25' THEN 2
                     WHEN '25-50' THEN 3 WHEN '>50' THEN 4 ELSE 5 END;

-- (b) Shadow evaluations: per (event, side) the FIRST in-window 0.74 trigger,
-- bucketed by the directional delta at that trigger, against the actual
-- market resolution. Includes triggers seen while paused / killed / outside
-- trading hours (see trading_state). Only resolved events are counted.
WITH first_trigger AS (
    SELECT e.*
    FROM live_btc_gate_evaluations e
    WHERE e.source = 'SHADOW'
      AND e.first_seen_at = (
          SELECT MIN(x.first_seen_at) FROM live_btc_gate_evaluations x
          WHERE x.source = 'SHADOW' AND x.event_id = e.event_id AND x.side = e.side
      )
),
joined AS (
    SELECT
        CAST(t.first_directional_delta_usd AS REAL) AS delta,
        t.side_outcome,
        r.actual_outcome,
        r.derived_outcome
    FROM first_trigger t
    JOIN live_btc_event_reference r ON r.event_id = t.event_id
    WHERE r.actual_outcome IN ('UP','DOWN')
),
bucketed AS (
    SELECT *,
        CASE
            WHEN delta IS NULL THEN 'NULL'
            WHEN delta < 0 THEN '<0'
            WHEN delta < 10 THEN '0-10'
            WHEN delta < 25 THEN '10-25'
            WHEN delta <= 50 THEN '25-50'
            ELSE '>50'
        END AS bucket
    FROM joined
)
SELECT
    bucket,
    COUNT(*) AS triggers,
    SUM(side_outcome = actual_outcome) AS trigger_side_won,
    SUM(side_outcome IS NOT NULL AND side_outcome <> actual_outcome) AS trigger_side_lost,
    SUM(side_outcome IS NULL) AS side_unknown,
    ROUND(100.0 * SUM(side_outcome = actual_outcome)
          / NULLIF(SUM(side_outcome IS NOT NULL), 0), 1) AS win_rate_pct
FROM bucketed
GROUP BY bucket
ORDER BY CASE bucket WHEN '<0' THEN 0 WHEN '0-10' THEN 1 WHEN '10-25' THEN 2
                     WHEN '25-50' THEN 3 WHEN '>50' THEN 4 ELSE 5 END;

-- (c) Sanity: how often each derived outcome matches the official resolution.
--   derived_outcome           : close 60s TWAP >= open spot (the stored default)
--   derived_outcome_spot      : close spot      >= open spot
--   derived_outcome_twap_open : close 60s TWAP >= open 60s TWAP
-- Gamma resolves on the Chainlink TWAP stream; which value counts as "the
-- price at the beginning" is not stated, so the two TWAP readings are kept.
SELECT
    COUNT(*) AS resolved_windows,
    SUM(outcome_match = 1) AS matches,
    SUM(outcome_match = 0) AS mismatches,
    SUM(derived_outcome = 'UNKNOWN') AS derived_unknown,
    ROUND(100.0 * SUM(outcome_match = 1) / NULLIF(SUM(outcome_match IS NOT NULL), 0), 2) AS match_pct,
    ROUND(100.0 * SUM(derived_outcome_spot = actual_outcome)
          / NULLIF(SUM(derived_outcome_spot <> 'UNKNOWN'), 0), 2) AS spot_match_pct,
    ROUND(100.0 * SUM(derived_outcome_twap_open = actual_outcome)
          / NULLIF(SUM(derived_outcome_twap_open <> 'UNKNOWN'), 0), 2) AS twap_open_match_pct,
    SUM(feed_gaps_in_window) AS gaps,
    SUM(feed_stalls_in_window) AS stalls
FROM live_btc_event_reference
WHERE actual_outcome IS NOT NULL;

-- (d) Entries missed for lack of a re-check after a gate block.
-- Today the gate is evaluated only on a fresh 0.74 edge. For every blocked
-- (enforce) or would-block (observe) evaluation an in-memory watch followed
-- the token until the ask left 0.74, the 60 s entry cutoff, or the window end,
-- and recorded whether the gate WOULD have passed meanwhile (crossed = 1).
--   DECISION rows: triggers that reached the entry decision. In enforce mode a
--                  crossing is a real missed entry; in observe mode the entry
--                  happened anyway, so the row is informational.
--   SHADOW rows  : every in-window trigger, whatever the trading state
--                  (paused, kill switch, schedule) -- see trading_state.
-- "Outcome" is the market resolution of the blocked side, i.e. the result had
-- the position been held to the end. TP/STOP paths of a hypothetical entry are
-- not knowable from this data. A row aggregates every 0.74 episode with the
-- same blocking reason; a crossing in any episode marks it crossed.
WITH watched AS (
    SELECT
        e.source, e.mode, e.side_outcome,
        e.delta_crossed_min_while_at_074 AS crossed,
        e.watch_end_reason,
        CAST(e.directional_delta_at_cross AS REAL) AS delta_at_cross,
        (julianday(e.crossed_at) - julianday(e.first_seen_at)) * 86400.0 AS seconds_to_cross,
        r.actual_outcome
    FROM live_btc_gate_evaluations e
    LEFT JOIN live_btc_event_reference r ON r.event_id = e.event_id
    WHERE e.allowed = 0
      AND e.watch_end_reason IS NOT NULL
      AND e.watch_end_reason <> 'SHUTDOWN'
)
SELECT
    source,
    mode,
    COUNT(*) AS blocked_watched,
    SUM(crossed = 1) AS missed_entries,
    ROUND(100.0 * SUM(crossed = 1) / COUNT(*), 1) AS missed_pct,
    SUM(crossed = 1 AND actual_outcome IS NOT NULL) AS missed_resolved,
    SUM(crossed = 1 AND side_outcome = actual_outcome) AS missed_side_won,
    SUM(crossed = 1 AND actual_outcome IS NOT NULL
        AND side_outcome <> actual_outcome) AS missed_side_lost,
    ROUND(100.0 * SUM(crossed = 1 AND side_outcome = actual_outcome)
          / NULLIF(SUM(crossed = 1 AND actual_outcome IS NOT NULL), 0), 1) AS missed_win_rate_pct,
    ROUND(AVG(CASE WHEN crossed = 1 THEN delta_at_cross END), 2) AS avg_delta_at_cross_usd,
    ROUND(AVG(CASE WHEN crossed = 1 THEN seconds_to_cross END), 1) AS avg_seconds_to_cross,
    SUM(watch_end_reason = 'ASK_LEFT_074') AS ended_ask_left,
    SUM(watch_end_reason = 'FINAL_MINUTE') AS ended_final_minute,
    SUM(watch_end_reason = 'WINDOW_ENDED') AS ended_window
FROM watched
GROUP BY source, mode
ORDER BY source, mode;
