"""Offline root-cause classifier for latched stop / emergency exits.

Pure function over a ``live_strategy_exit_audit`` row plus its
``live_strategy_exit_book_snapshots``. No I/O. Deterministic: the same inputs
always yield the same classification, so two analysts (or a re-run) agree.

Two independent axes are returned:

* ``root_cause`` / ``primary_cause`` — MARKET / LIQUIDITY / DETECTION_DELAY /
  EXECUTION_DELAY / BOOK_FILL_MISMATCH, decided by loss attribution
  (``primary_cause = argmax`` of the loss components). ``MIXED`` when the top two
  components are comparable; ``UNKNOWN`` when the evidence needed to decide is
  missing.
* ``exit_outcome`` — ACCEPTABLE / BAD_EXIT / NO_EXECUTION, purely on realized
  price vs the acceptable cutoff. A good price never launders a slow execution.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .book_walk import book_walk_vwap, normalize_bid_levels

CLASSIFIER_VERSION = "exit-classifier-v1"

_ZERO = Decimal("0")


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    return d if d.is_finite() else None


def _pos(value: Decimal | None) -> Decimal:
    return value if value is not None and value > _ZERO else _ZERO


def classify_exit(
    row: dict[str, Any],
    snapshots: list[dict[str, Any]] | None = None,
    *,
    acceptable_min_vwap: Decimal | str | float = "0.60",
    sla_ms: int = 2000,
    negligible_loss: Decimal | str | float = "0.01",
    mixed_ratio: Decimal | str | float = "0.60",
    fresh_book_max_age_ms: int = 1500,
) -> dict[str, Any]:
    accept = _dec(acceptable_min_vwap) or Decimal("0.60")
    eps = _dec(negligible_loss) or Decimal("0.01")
    ratio = _dec(mixed_ratio) or Decimal("0.60")
    snaps = {str(s.get("phase")): s for s in (snapshots or [])}

    p_cross = _dec(row.get("cross_best_bid_text"))
    p_latch = _dec(row.get("latch_best_bid_text"))
    p_submit = _dec(row.get("submit_best_bid_text"))
    actual_vwap = _dec(row.get("actual_fill_vwap_text"))
    requested = _dec(row.get("requested_shares_text"))
    detection_ms = _int(row.get("detection_latency_ms"))
    execution_ms = _int(row.get("execution_latency_ms"))
    settlement_wait = bool(_int(row.get("settlement_wait")))

    has_fill = bool(actual_vwap and actual_vwap > _ZERO)

    # -------- outcome axis --------
    if not has_fill:
        outcome = "NO_EXECUTION"
    elif actual_vwap >= accept:
        outcome = "ACCEPTABLE"
    else:
        outcome = "BAD_EXIT"

    reasons: list[str] = []

    # -------- no execution --------
    if not has_fill:
        status = str(row.get("clob_status") or "").upper()
        submit_book = _fresh_book(snaps.get("SUBMIT"), fresh_book_max_age_ms)
        depth_ok = _depth_covers(submit_book, requested, p_submit)
        if any(k in status for k in ("REJECT", "ERROR", "INVALID", "PROTECT")):
            root = "EXECUTION_DELAY"
            reasons.append(f"order rejected ({status or 'unknown'})")
        elif "FAK" in status or "NOT_FILLED" in status or "ZERO" in status:
            if depth_ok is False:
                root = "LIQUIDITY"
                reasons.append("FAK took nothing; submit book lacked depth above the floor")
            else:
                root = "MARKET"
                reasons.append("FAK took nothing though the book looked adequate")
        else:
            root = "UNKNOWN"
            reasons.append("no fill and no usable clob_status")
        return _result(root, root, _tech(root), outcome, reasons, row)

    # -------- attribution --------
    loss_detection = _pos(p_cross - p_latch) if (p_cross and p_latch) else None
    loss_execution = _pos(p_latch - p_submit) if (p_latch and p_submit) else None

    expected_vwap = _dec(row.get("expected_vwap_at_submit_text"))
    if expected_vwap is None:
        expected_vwap = _expected_from_snapshot(
            snaps.get("SUBMIT") or snaps.get("NEAR_SUBMIT_REFRESH"), requested, p_submit
        )
    loss_fill = (
        _pos(expected_vwap - actual_vwap) if expected_vwap is not None else None
    )

    if loss_detection is None and loss_execution is None and loss_fill is None:
        # No book prices (typical for reconstructed history). Fall back to
        # latency alone, low confidence.
        if outcome == "ACCEPTABLE" and (
            (detection_ms is None or detection_ms <= sla_ms)
            and (execution_ms is None or execution_ms <= sla_ms)
        ):
            return _result(
                "HEALTHY", "HEALTHY", "HEALTHY", outcome,
                ["no book depth, but latencies within SLA and price acceptable"],
                row,
            )
        if execution_ms is not None and execution_ms > sla_ms:
            tech = "SETTLEMENT_WAIT" if settlement_wait else "DELAY"
            note = (
                "on-chain entry settlement had not completed (expected)"
                if settlement_wait
                else "no book depth to attribute the price loss"
            )
            return _result(
                "EXECUTION_DELAY", "EXECUTION_DELAY", tech, outcome,
                [f"latency-only: SELL {execution_ms}ms after latch; {note}"],
                row,
            )
        if detection_ms is not None and detection_ms > sla_ms:
            return _result(
                "DETECTION_DELAY", "DETECTION_DELAY", "DELAY", outcome,
                [f"latency-only: latch {detection_ms}ms after the cross frame; "
                 "no book depth to attribute the price loss"],
                row,
            )
        return _result(
            "UNKNOWN", "UNKNOWN", "UNKNOWN", outcome,
            ["no book prices captured at cross / latch / submit"], row,
        )

    components = {
        "DETECTION_DELAY": loss_detection or _ZERO,
        "EXECUTION_DELAY": loss_execution or _ZERO,
        "FILL": loss_fill or _ZERO,
    }
    ranked = sorted(components.items(), key=lambda kv: kv[1], reverse=True)
    top_name, top_loss = ranked[0]
    second_name, second_loss = ranked[1]

    within_sla = (
        (detection_ms is None or detection_ms <= sla_ms)
        and (execution_ms is None or execution_ms <= sla_ms)
    )
    if top_loss <= eps:
        if within_sla:
            reasons.append(
                "all loss components negligible and latencies within SLA"
            )
            return _result(
                "HEALTHY", "HEALTHY", "HEALTHY", outcome, reasons, row
            )
        # No measurable price damage, but a latency breached SLA. Name the
        # latency that actually breached — do NOT argmax a set of zeros.
        exec_breach = execution_ms is not None and execution_ms > sla_ms
        det_breach = detection_ms is not None and detection_ms > sla_ms
        if exec_breach:
            tech = "SETTLEMENT_WAIT" if settlement_wait else "DELAY"
            reasons.append(
                f"SELL {execution_ms}ms after latch (> SLA); no price damage — "
                + ("on-chain settlement wait (expected)" if settlement_wait
                   else "slow submit, price held")
            )
            return _result(
                "EXECUTION_DELAY", "EXECUTION_DELAY", tech, outcome, reasons,
                row, components=components,
            )
        if det_breach:
            reasons.append(
                f"latch {detection_ms}ms after the cross frame (> SLA); "
                "no price damage — slow detection, price held"
            )
            return _result(
                "DETECTION_DELAY", "DETECTION_DELAY", "DELAY", outcome, reasons,
                row, components=components,
            )

    primary = _resolve_component(
        top_name, row, snaps, requested, p_submit,
        detection_ms, execution_ms, sla_ms, fresh_book_max_age_ms, reasons,
    )

    mixed = (
        second_loss > eps
        and top_loss > _ZERO
        and (second_loss / top_loss) >= ratio
    )
    if mixed:
        secondary = _resolve_component(
            second_name, row, snaps, requested, p_submit,
            detection_ms, execution_ms, sla_ms, fresh_book_max_age_ms, reasons,
        )
        reasons.append(
            f"second component {second_name} within {ratio} of the primary"
        )
        return _result(
            "MIXED", primary, "MIXED", outcome, reasons, row,
            components=components, secondary=secondary,
        )

    tech = (
        "SETTLEMENT_WAIT"
        if primary == "EXECUTION_DELAY" and settlement_wait
        else _tech(primary)
    )
    return _result(
        primary, primary, tech, outcome, reasons, row,
        components=components,
    )


# --------------------------------------------------------------------------- #
def _resolve_component(
    name: str, row, snaps, requested, p_submit,
    detection_ms, execution_ms, sla_ms, fresh_max_age, reasons,
) -> str:
    if name == "DETECTION_DELAY":
        # Exchange-time elapsed between the cross book and the latch book. If the
        # price moved over real market time we tracked it (MARKET); if our wall
        # clock ran far ahead of exchange time, our processing lagged the feed
        # (DETECTION_DELAY / stale data).
        x_cross = _int(row.get("cross_exchange_timestamp_ms"))
        x_latch = _int(row.get("latch_exchange_timestamp_ms"))
        exch_ms = (x_latch - x_cross) if (x_cross and x_latch) else None
        if exch_ms is not None and detection_ms is not None:
            if detection_ms - exch_ms > sla_ms:
                reasons.append(
                    f"wall cross->latch {detection_ms}ms vs exchange-time "
                    f"{exch_ms}ms: our processing lagged the feed"
                )
                return "DETECTION_DELAY"
            reasons.append(
                f"cross->latch {detection_ms}ms tracks exchange-time {exch_ms}ms: "
                "the market moved, not our detection"
            )
            return "MARKET"
        if detection_ms is not None and detection_ms <= sla_ms:
            reasons.append(
                f"price fell {row.get('loss_detection_text')} cross->latch in "
                f"{detection_ms}ms (< SLA): market gap, not detection lag"
            )
            return "MARKET"
        reasons.append(
            f"latch came {detection_ms}ms after the cross frame"
        )
        return "DETECTION_DELAY"
    if name == "EXECUTION_DELAY":
        if execution_ms is not None and execution_ms <= sla_ms:
            reasons.append(
                f"price fell {row.get('loss_execution_text')} latch->submit in "
                f"{execution_ms}ms (< SLA): market gap, not execution lag"
            )
            return "MARKET"
        if _int(row.get("settlement_wait")):
            reasons.append(
                f"SELL blocked {execution_ms}ms waiting for on-chain entry "
                "settlement (expected, not a code delay)"
            )
        else:
            reasons.append(f"SELL submitted {execution_ms}ms after the latch")
        return "EXECUTION_DELAY"
    # FILL
    submit_book = _fresh_book(
        snaps.get("NEAR_SUBMIT_REFRESH") or snaps.get("SUBMIT"), fresh_max_age
    )
    depth_ok = _depth_covers(submit_book, requested, p_submit)
    if depth_ok is True:
        reasons.append(
            "fresh submit-time book had ample depth at a good price yet the "
            "fill was materially worse"
        )
        return "BOOK_FILL_MISMATCH"
    if depth_ok is False:
        reasons.append("submit-time book lacked depth for our size above the floor")
        return "LIQUIDITY"
    reasons.append("fill worse than book-implied, but no fresh submit book to judge depth")
    return "MARKET"


def _tech(root: str) -> str:
    return {
        "DETECTION_DELAY": "DELAY",
        "EXECUTION_DELAY": "DELAY",
        "MARKET": "MARKET",
        "LIQUIDITY": "LIQUIDITY",
        "BOOK_FILL_MISMATCH": "MISMATCH",
        "HEALTHY": "HEALTHY",
        "UNKNOWN": "UNKNOWN",
    }.get(root, "UNKNOWN")


def _result(root, primary, tech, outcome, reasons, row, **extra) -> dict[str, Any]:
    out = {
        "root_cause": root,
        "primary_cause": primary,
        "technical_behavior": tech,
        "exit_outcome": outcome,
        "classifier_version": CLASSIFIER_VERSION,
        "rationale": "; ".join(reasons) or "n/a",
    }
    if "components" in extra:
        out["loss_components"] = {
            k: format(v, "f") for k, v in extra["components"].items()
        }
    if extra.get("secondary"):
        out["secondary_cause"] = extra["secondary"]
    return out


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fresh_book(snapshot: dict[str, Any] | None, max_age_ms: int) -> dict | None:
    if not snapshot:
        return None
    age = _int(snapshot.get("exchange_age_ms"))
    if age is not None and age > max_age_ms:
        return None
    return snapshot


def _expected_from_snapshot(snapshot, size, best_bid) -> Decimal | None:
    if not snapshot or size is None:
        return None
    vwap, _method, _fillable = book_walk_vwap(
        snapshot.get("bids_json"), size, best_bid=best_bid
    )
    return vwap


def _depth_covers(snapshot, size, best_bid) -> bool | None:
    """True if the ladder can fill ``size`` near ``best_bid``; False if it
    clearly cannot; None if we cannot tell."""
    if not snapshot or size is None or size <= _ZERO:
        return None
    levels = normalize_bid_levels(snapshot.get("bids_json"))
    if not levels:
        return None
    available = sum((lvl[1] for lvl in levels), _ZERO)
    if available < size:
        return False
    vwap, method, fillable = book_walk_vwap(
        snapshot.get("bids_json"), size, best_bid=best_bid
    )
    if method != "ORDER_BOOK_VWAP" or vwap is None:
        return False
    top = levels[0][0]
    # "near best_bid" == VWAP within 3 cents of the visible top of book.
    return (top - vwap) <= Decimal("0.03")
