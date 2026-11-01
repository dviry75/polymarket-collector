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

-- (c) Sanity: how often the spot-derived outcome (close >= open on the
-- Chainlink spot feed) matches the official TWAP-based resolution.
SELECT
    COUNT(*) AS resolved_windows,
    SUM(outcome_match = 1) AS matches,
    SUM(outcome_match = 0) AS mismatches,
    SUM(derived_outcome = 'UNKNOWN') AS derived_unknown,
    ROUND(100.0 * SUM(outcome_match = 1) / NULLIF(SUM(outcome_match IS NOT NULL), 0), 2) AS match_pct,
    SUM(feed_gaps_in_window) AS gaps,
    SUM(feed_stalls_in_window) AS stalls
FROM live_btc_event_reference
WHERE actual_outcome IS NOT NULL;
