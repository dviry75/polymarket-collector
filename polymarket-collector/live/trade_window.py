"""Incremental, bounded, restart-safe account-trade fetching.

Reconciliation used to page through the account's entire trade history on
every run, so its cost grew with lifetime account activity rather than with
new activity. This module narrows the remote read to a watermark-anchored
window and enforces hard guardrails on how much work a single run may do.

Invariants that must not be weakened:

* The watermark advances only after every page in the planned window was
  fetched *and* the caller confirmed persistence. A partial window never
  advances it, so the next run re-reads the same range.
* Deduplication is by Trade ID, never by timestamp. The overlap deliberately
  re-reads a few already-known trades; the unique constraint on
  ``live_strategy_fills.remote_trade_id`` (and the unique index on
  ``live_order_fills.polymarket_trade_id``) is what makes that free.
* When guardrails truncate a window the range is not lost: the window is
  time-sliced and narrowed until it fits, and the watermark stays put.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import time
from typing import Any, Iterable


# Late trades, pagination boundaries and one-second ``match_time`` granularity
# all mean "after = last_seen" would silently drop fills. Five minutes is far
# larger than any propagation delay observed on this account while still being
# a handful of trades at the live trade rate.
DEFAULT_OVERLAP_SECONDS = 300.0
DEFAULT_MAX_PAGES = 25
DEFAULT_MAX_TRADES = 2_500
DEFAULT_TIME_BUDGET_SECONDS = 10.0
# A window is never sliced below this: at the live trade rate a minute cannot
# plausibly overflow the per-run caps, so hitting it means something is wrong
# and the run must fail loudly instead of skipping trades.
DEFAULT_MIN_SLICE_SECONDS = 60.0
DEFAULT_MAX_EMPTY_PAGES = 3
DEFAULT_BOOTSTRAP_MAX_PAGES = 500
DEFAULT_BOOTSTRAP_MAX_TRADES = 200_000
DEFAULT_BOOTSTRAP_TIME_BUDGET_SECONDS = 120.0

STATE_WATERMARK_AT = "trade_fetch_watermark_at"
STATE_WATERMARK_TRADE_ID = "trade_fetch_watermark_trade_id"
STATE_SLICE_SECONDS = "trade_fetch_slice_seconds"
STATE_WATERMARK_SOURCE = "trade_fetch_watermark_source"

WATERMARK_STATE_DEFAULTS = {
    STATE_WATERMARK_AT: "",
    STATE_WATERMARK_TRADE_ID: "",
    STATE_SLICE_SECONDS: "",
    STATE_WATERMARK_SOURCE: "",
}


class TradeWindowExhaustedError(RuntimeError):
    """A minimum-width slice still overflowed the per-run guardrails.

    Advancing past it would skip trades, so the run fails and reconciliation's
    normal backoff/retry path owns the outcome.
    """


def parse_timestamp(value: Any) -> datetime | None:
    """Accept the ISO strings we persist and the epochs the API may return."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        try:
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def to_epoch_seconds(value: datetime | None) -> int | None:
    return None if value is None else int(value.timestamp())


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class TradeWindowPolicy:
    """Tunables for one incremental fetch. All limits are per reconciliation run."""

    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS
    max_pages: int = DEFAULT_MAX_PAGES
    max_trades: int = DEFAULT_MAX_TRADES
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS
    min_slice_seconds: float = DEFAULT_MIN_SLICE_SECONDS
    max_empty_pages: int = DEFAULT_MAX_EMPTY_PAGES
    enabled: bool = True
    # A first-ever sweep on an empty database has no lower bound to slice
    # against, so it gets its own one-off budget instead of silently reading a
    # partial history. Exceeding even this fails the run loudly.
    bootstrap_max_pages: int = DEFAULT_BOOTSTRAP_MAX_PAGES
    bootstrap_max_trades: int = DEFAULT_BOOTSTRAP_MAX_TRADES
    bootstrap_time_budget_seconds: float = DEFAULT_BOOTSTRAP_TIME_BUDGET_SECONDS

    def for_bootstrap(self) -> "TradeWindowPolicy":
        return replace(
            self,
            max_pages=self.bootstrap_max_pages,
            max_trades=self.bootstrap_max_trades,
            time_budget_seconds=self.bootstrap_time_budget_seconds,
        )

    @classmethod
    def from_config(cls, config: Any) -> "TradeWindowPolicy":
        return cls(
            overlap_seconds=float(
                getattr(config, "trade_fetch_overlap_seconds", DEFAULT_OVERLAP_SECONDS)
            ),
            max_pages=int(getattr(config, "trade_fetch_max_pages", DEFAULT_MAX_PAGES)),
            max_trades=int(
                getattr(config, "trade_fetch_max_trades", DEFAULT_MAX_TRADES)
            ),
            time_budget_seconds=float(
                getattr(
                    config,
                    "trade_fetch_time_budget_seconds",
                    DEFAULT_TIME_BUDGET_SECONDS,
                )
            ),
            min_slice_seconds=float(
                getattr(
                    config, "trade_fetch_min_slice_seconds", DEFAULT_MIN_SLICE_SECONDS
                )
            ),
            enabled=bool(getattr(config, "trade_fetch_incremental_enabled", True)),
        )


@dataclass(frozen=True)
class TradeWindowPlan:
    """The range this run is allowed to read, plus how it was derived."""

    after_at: datetime | None
    before_at: datetime | None
    watermark_at: datetime | None
    slice_seconds: float | None
    bootstrap: bool
    planned_at: datetime

    @property
    def after_epoch(self) -> int | None:
        return to_epoch_seconds(self.after_at)

    @property
    def before_epoch(self) -> int | None:
        return to_epoch_seconds(self.before_at)

    @property
    def full_history(self) -> bool:
        return self.after_at is None


@dataclass
class TradeWindowResult:
    plan: TradeWindowPlan
    trades: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    remote_count: int = 0
    duplicate_in_batch: int = 0
    duration_ms: float = 0.0
    truncated: bool = False
    limit_reason: str = ""
    max_matched_at: datetime | None = None
    max_matched_trade_id: str = ""

    @property
    def new_count(self) -> int:
        return len(self.trades)


def read_watermark_state(states: dict[str, str]) -> tuple[datetime | None, float | None]:
    """Decode the persisted watermark/slice pair, tolerating absent or junk values."""
    watermark = parse_timestamp(states.get(STATE_WATERMARK_AT) or "")
    raw_slice = str(states.get(STATE_SLICE_SECONDS) or "").strip()
    try:
        slice_seconds = float(raw_slice) if raw_slice else None
    except ValueError:
        slice_seconds = None
    if slice_seconds is not None and slice_seconds <= 0:
        slice_seconds = None
    return watermark, slice_seconds


def bootstrap_watermark(
    latest_local_trade_at: Any,
    policy: TradeWindowPolicy,
) -> tuple[datetime | None, str]:
    """Derive a first watermark from what the local DB already proves it holds.

    A populated DB needs no historical refetch: everything up to the newest
    safely persisted trade is already ours, so the watermark starts one overlap
    behind it. An empty DB gets no watermark at all, which plans an explicit,
    guardrail-bounded bootstrap sweep instead of a silent partial read.
    """
    latest = parse_timestamp(latest_local_trade_at)
    if latest is None:
        return None, "empty_database_bootstrap"
    return latest - timedelta(seconds=policy.overlap_seconds), "local_fill_history"


def plan_trade_window(
    *,
    watermark_at: datetime | None,
    slice_seconds: float | None,
    policy: TradeWindowPolicy,
    now: datetime | None = None,
) -> TradeWindowPlan:
    planned_at = now or datetime.now(timezone.utc)
    if watermark_at is None:
        return TradeWindowPlan(
            after_at=None,
            before_at=None,
            watermark_at=None,
            slice_seconds=None,
            bootstrap=True,
            planned_at=planned_at,
        )
    after_at = watermark_at - timedelta(seconds=policy.overlap_seconds)
    before_at = (
        after_at + timedelta(seconds=slice_seconds)
        if slice_seconds is not None
        else None
    )
    if before_at is not None and before_at >= planned_at:
        # The narrowed slice already reaches the present; an open-ended window
        # is cheaper and lets the slice reset on success.
        before_at = None
        slice_seconds = None
    return TradeWindowPlan(
        after_at=after_at,
        before_at=before_at,
        watermark_at=watermark_at,
        slice_seconds=slice_seconds,
        bootstrap=False,
        planned_at=planned_at,
    )


async def fetch_trade_window(
    adapter: Any,
    plan: TradeWindowPlan,
    policy: TradeWindowPolicy,
    *,
    monotonic: Any = time.monotonic,
) -> TradeWindowResult:
    """Page through one planned window under hard guardrails.

    Stops on: exhausted pages, page cap, trade cap, time budget, a repeated
    cursor, or a run of pages that make no progress. Every stop other than
    "exhausted" marks the result truncated, which the caller must treat as
    "do not advance the watermark".
    """
    result = TradeWindowResult(plan=plan)
    started = monotonic()
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None
    empty_pages = 0

    while True:
        if result.pages >= policy.max_pages:
            result.truncated = True
            result.limit_reason = "max_pages"
            break
        if monotonic() - started >= policy.time_budget_seconds:
            result.truncated = True
            result.limit_reason = "time_budget"
            break

        page = await _fetch_page(
            adapter,
            after=plan.after_epoch,
            before=plan.before_epoch,
            cursor=cursor,
        )
        result.pages += 1
        items = list(page.get("trades") or [])
        result.remote_count += len(items)

        for trade in items:
            trade_id = str(trade.get("polymarket_trade_id") or "")
            if trade_id and trade_id in seen_ids:
                # Overlapping pages are normal at a cursor boundary; keeping one
                # copy is what makes re-reads free.
                result.duplicate_in_batch += 1
                continue
            if trade_id:
                seen_ids.add(trade_id)
            result.trades.append(trade)
            matched_at = parse_timestamp(trade.get("matched_at"))
            if matched_at is not None and (
                result.max_matched_at is None or matched_at > result.max_matched_at
            ):
                result.max_matched_at = matched_at
                result.max_matched_trade_id = trade_id

        if len(result.trades) >= policy.max_trades:
            result.truncated = True
            result.limit_reason = "max_trades"
            break
        if not page.get("has_more"):
            break

        next_cursor = page.get("next_cursor")
        next_cursor = str(next_cursor) if next_cursor else ""
        if not next_cursor or next_cursor in seen_cursors:
            # An API that keeps handing back a cursor it already gave us would
            # otherwise loop forever; the SDK paginator has no such guard.
            result.truncated = True
            result.limit_reason = "repeated_cursor"
            break
        seen_cursors.add(next_cursor)

        if not items:
            empty_pages += 1
            if empty_pages >= policy.max_empty_pages:
                result.truncated = True
                result.limit_reason = "no_progress"
                break
        else:
            empty_pages = 0
        cursor = next_cursor

    result.duration_ms = max(0.0, (monotonic() - started) * 1000.0)
    return result


async def _fetch_page(
    adapter: Any,
    *,
    after: int | None,
    before: int | None,
    cursor: str | None,
) -> dict[str, Any]:
    page_reader = getattr(adapter, "get_trades_page", None)
    if page_reader is None:
        # Adapters that predate windowing still work: one page, no filter, and
        # the caller's Trade-ID dedup keeps the result correct.
        return {"trades": list(await adapter.get_trades()), "has_more": False}
    return await page_reader(
        after=str(after) if after is not None else None,
        before=str(before) if before is not None else None,
        cursor=cursor,
    )


def next_watermark_state(
    plan: TradeWindowPlan,
    result: TradeWindowResult,
    policy: TradeWindowPolicy,
) -> dict[str, str]:
    """Decide the watermark/slice to persist once the caller confirms persistence.

    Truncated windows never advance the watermark; they narrow the slice so the
    next run covers a strictly smaller range and eventually drains the backlog.
    """
    if result.truncated and plan.bootstrap:
        # No watermark and no lower bound: there is nothing to narrow, and any
        # advance would claim coverage the run never read.
        raise TradeWindowExhaustedError(
            "bootstrap account-trade sweep exceeded its budget "
            f"({result.limit_reason}); run an explicit historical backfill"
        )
    if result.truncated:
        current = plan.slice_seconds
        if current is None:
            span = (plan.planned_at - plan.after_at).total_seconds() if plan.after_at else 0.0
            candidate = max(policy.min_slice_seconds, span / 2.0)
        else:
            candidate = current / 2.0
        if candidate < policy.min_slice_seconds:
            if current is not None and current <= policy.min_slice_seconds:
                raise TradeWindowExhaustedError(
                    "minimum trade window slice still exceeds per-run limits "
                    f"({result.limit_reason})"
                )
            candidate = policy.min_slice_seconds
        return {STATE_SLICE_SECONDS: f"{candidate:.3f}"}

    if plan.before_at is not None:
        # A bounded slice drained completely, so the whole slice is covered even
        # where it held no trades.
        advanced = plan.before_at
    else:
        # An open-ended window drained completely, so everything up to the
        # moment the read started is covered. The overlap on the next run is
        # what absorbs trades that land late with an older match_time.
        advanced = plan.planned_at
    if result.max_matched_at is not None and result.max_matched_at > advanced:
        advanced = result.max_matched_at
    if plan.watermark_at is not None and advanced < plan.watermark_at:
        advanced = plan.watermark_at

    state = {
        STATE_WATERMARK_AT: _iso(advanced),
        STATE_SLICE_SECONDS: "",
    }
    if result.max_matched_trade_id:
        state[STATE_WATERMARK_TRADE_ID] = result.max_matched_trade_id
    return state


def telemetry(
    plan: TradeWindowPlan,
    result: TradeWindowResult,
    policy: TradeWindowPolicy,
    *,
    duplicate_count: int,
) -> dict[str, Any]:
    """The metric names the operator asked to be able to read per run."""
    return {
        "trade_fetch_after": _iso(plan.after_at) if plan.after_at else "",
        "trade_fetch_before": _iso(plan.before_at) if plan.before_at else "",
        "trade_fetch_overlap_seconds": policy.overlap_seconds,
        "trade_fetch_pages": result.pages,
        "trade_fetch_remote_count": result.remote_count,
        "trade_fetch_new_count": max(0, result.new_count - duplicate_count),
        "trade_fetch_duplicate_count": duplicate_count + result.duplicate_in_batch,
        "trade_fetch_duration_ms": round(result.duration_ms, 3),
        "trade_fetch_backlog_or_limit_hit": result.limit_reason if result.truncated else "",
        "trade_fetch_bootstrap": plan.bootstrap,
        "trade_fetch_slice_seconds": plan.slice_seconds or 0.0,
    }


def dedupe_by_trade_id(trades: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first record per Trade ID; records without an ID all survive."""
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for trade in trades:
        trade_id = str(trade.get("polymarket_trade_id") or "")
        if trade_id:
            if trade_id in seen:
                continue
            seen.add(trade_id)
        output.append(trade)
    return output
