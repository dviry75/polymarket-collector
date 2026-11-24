"""The dashboard's "היסטוריית עסקאות" table as an Excel sheet ("עסקאות").

Rows come from the same read model and endpoint data the dashboard table
uses (``DashboardReadModel.external_deal_history``, range "all"), and every
column mirrors the dashboard cell logic in ``app/page.tsx`` of the
polymarket-dashboard repo (status labels, entry liquidity, BTC moves, market
outcome). Keep the two in step when either changes.

Columns 1-17 are the dashboard columns, in the dashboard's order. The ones
after them are extra detail the dashboard keeps in tooltips.
"""

from __future__ import annotations

from datetime import datetime
import logging
import math
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .dashboard_read_model import EXTERNAL_DEALS_MAX_PAGE_SIZE, DashboardReadModel, resolve_window
from .repository import LiveRepository

log = logging.getLogger(__name__)

SHEET_TITLE = "עסקאות"
TZ = ZoneInfo("Asia/Jerusalem")
GREEN = "0B8060"
RED = "C62828"

# Dashboard table columns, same order and titles.
TABLE_COLUMNS = (
    "#", "התחלה", "סיום", "שוק", "טוקן", "פעולות", "קנייה $", "מחיר קנייה",
    "מכירה $", "מחיר מכירה", "עמלות", "תזרים", "נזילות כניסה", "BTC בכניסה",
    "BTC ביציאה", "תוצאת שוק", "סטטוס",
)
# Tooltip detail from the dashboard, as plain columns.
EXTRA_COLUMNS = (
    "BTC לטובת העסקה בכניסה", "BTC לטובת העסקה ביציאה", "BTC פתיחה (price to beat)",
    "BTC בכניסה (מחיר)", "BTC ביציאה (מחיר)", "סוג יציאה BTC", "שער BTC",
    "סטטוס (קוד)", "מצב", "Event Slug",
)
COLUMNS = TABLE_COLUMNS + EXTRA_COLUMNS

STATUS_LABEL = {
    "HEALTHY": "תקין", "DETECTION_DELAY": "איחור זיהוי", "EXECUTION_DELAY": "איחור ביצוע",
    "MARKET": "קפיצת שוק", "LIQUIDITY": "נזילות", "BOOK_FILL_MISMATCH": "פער ספר/מילוי",
    "MIXED": "משולב", "UNKNOWN": "לא ידוע",
}
STATUS_TONE = {
    "DETECTION_DELAY": "negative", "EXECUTION_DELAY": "negative",
    "BOOK_FILL_MISMATCH": "negative", "HEALTHY": "positive",
}


# ------------------------------------------------------------ value helpers
def _text(value: Any) -> str:
    return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _num(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _bool(value: Any) -> bool:
    return value is True or value == 1 or str(value).lower() == "true"


def _fmt(value: float) -> str:
    """Up to 4 decimals, no trailing zeros (the dashboard's number format)."""
    return f"{value:.4f}".rstrip("0").rstrip(".") if value != int(value) else str(int(value))


def _local(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return stamp
    return stamp.astimezone(TZ).replace(tzinfo=None)  # Excel has no time zones


def _price(notional: Any, size: Any) -> float | None:
    n, s = _num(notional), _num(size)
    return None if n is None or s is None or s == 0 else n / s


# ------------------------------------------------- dashboard cell mirrors
def deal_status(row: dict[str, Any]) -> tuple[str, str, str]:
    """(label, tone, code) — mirrors dealStatus() in page.tsx."""
    root = _text(row.get("exit_root_cause"))
    if not root:
        return "—", "", ""
    tech = _text(row.get("exit_technical_behavior"))
    outcome = _text(row.get("exit_analysis_outcome"))
    reconstructed = _text(row.get("exit_evidence_quality")).startswith("RECONSTRUCTED")
    if tech == "SETTLEMENT_WAIT":
        key, label, tone = "SETTLEMENT_WAIT", "המתנה לסליקה", "positive"
    elif outcome == "NO_EXECUTION":
        key, label, tone = "NO_EXECUTION", "לא בוצע", "negative"
    elif tech == "MIXED":
        key, label = "MIXED", "משולב"
        tone = STATUS_TONE.get(_text(row.get("exit_primary_cause")), "")
    else:
        key, label, tone = root, STATUS_LABEL.get(root, root), STATUS_TONE.get(root, "")
    return label + (" ~" if reconstructed else ""), tone, key


def entry_liquidity(row: dict[str, Any]) -> tuple[str, str]:
    """(text, tone) — mirrors entryLiquidityCell() in page.tsx."""
    vwap = _num(row.get("entry_sim_exit_vwap"))
    worst = _num(row.get("entry_sim_exit_worst_price"))
    size = _num(row.get("entry_sim_exit_shares"))
    fillable = _num(row.get("entry_sim_exit_fillable_1x"))
    if vwap is None and worst is None and fillable is None:
        return "—", ""
    deep = _bool(row.get("entry_liquidity_deep_capture"))
    partial = size is not None and fillable is not None and fillable + 1e-9 < size
    tone = "negative" if deep or partial or (worst is not None and worst < 0.6) else ""
    sync = _bool(row.get("entry_liquidity_synchronous"))
    head = _fmt(worst) if worst is not None else _fmt(vwap) if vwap is not None else "—"
    ratio = f" · {_fmt(fillable)}/{_fmt(size)}" if size is not None and fillable is not None else ""
    return f"{head}{ratio}{'' if sync else ' ~'}", tone


def btc_move(btc: dict[str, Any], price_key: str) -> float | None:
    """BTC's real move since the event open (price to beat), like btcMove()."""
    open_price, at = _num(btc.get("btc_open_price")), _num(btc.get(price_key))
    return at - open_price if open_price is not None and at is not None else None


def market_result(row: dict[str, Any]) -> tuple[str, str]:
    """(UP/DOWN/"—", tone) — mirrors marketOutcomeCell() in page.tsx."""
    winner = _text(row.get("market_outcome")).strip().upper()
    if not winner:
        return "—", ""
    bought = [v.strip().upper() for v in _text(row.get("outcomes")).split(",") if v.strip()]
    if len(bought) != 1:
        return winner, ""
    return winner, "positive" if bought[0] == winner else "negative"


def table_row(item: dict[str, Any]) -> dict[str, tuple[Any, str]]:
    """One deal as {column: (value, tone)}."""
    btc = item.get("btc") if isinstance(item.get("btc"), dict) else {}
    cash_flow = _num(item.get("cash_flow_usd"))
    liquidity, liquidity_tone = entry_liquidity(item)
    entry_move = btc_move(btc, "btc_entry_price")
    exit_move = btc_move(btc, "btc_exit_price")
    exit_type = _text(btc.get("btc_exit_type"))
    outcome, outcome_tone = market_result(item)
    status, status_tone, status_code = deal_status(item)
    gate = _text(btc.get("btc_gate_reason"))
    mode = _text(btc.get("btc_gate_mode"))

    def tone_of(value: float | None) -> str:
        return "" if value is None else "positive" if value >= 0 else "negative"

    return {
        "#": (int(item.get("seq") or 0) or None, ""),
        "התחלה": (_local(item.get("first_action_at")), ""),
        "סיום": (_local(item.get("last_action_at")), ""),
        "שוק": (_text(item.get("market_title")) or _text(item.get("event_slug"))
                or _text(item.get("deal_key")), ""),
        "טוקן": (_text(item.get("outcomes")) or "—", ""),
        "פעולות": (int(item.get("action_count") or 0), ""),
        "קנייה $": (_num(item.get("buy_notional_usd")), ""),
        "מחיר קנייה": (_price(item.get("buy_notional_usd"), item.get("buy_size")), ""),
        "מכירה $": (_num(item.get("sell_notional_usd")), ""),
        "מחיר מכירה": (_price(item.get("sell_notional_usd"), item.get("sell_size")), ""),
        "עמלות": (_num(item.get("fees_usd")), ""),
        "תזרים": (cash_flow, "positive" if (cash_flow or 0) >= 0 else "negative"),
        "נזילות כניסה": (liquidity, liquidity_tone),
        "BTC בכניסה": (entry_move, tone_of(entry_move)),
        "BTC ביציאה": (exit_move, tone_of(exit_move)),
        "תוצאת שוק": (outcome, outcome_tone),
        "סטטוס": (status, status_tone),
        "BTC לטובת העסקה בכניסה": (_num(btc.get("btc_entry_directional_delta_usd")), ""),
        "BTC לטובת העסקה ביציאה": (_num(btc.get("btc_exit_directional_delta_usd")), ""),
        "BTC פתיחה (price to beat)": (_num(btc.get("btc_open_price")), ""),
        "BTC בכניסה (מחיר)": (_num(btc.get("btc_entry_price")), ""),
        "BTC ביציאה (מחיר)": (_num(btc.get("btc_exit_price")), ""),
        "סוג יציאה BTC": ("הכרעה" if exit_type == "RESOLUTION" else exit_type, ""),
        "שער BTC": (f"{gate} ({mode})" if mode else "", ""),
        "סטטוס (קוד)": (status_code, ""),
        "מצב": (_text(item.get("state")), ""),
        "Event Slug": (_text(item.get("event_slug")), ""),
    }


# ------------------------------------------------------------ data + sheet
def load_deal_items(db_path: str | Path) -> tuple[list[dict[str, Any]], str]:
    """All deals (range "all"), newest first — what the dashboard table loads."""
    try:
        model = DashboardReadModel(LiveRepository(db_path, query_only=True))
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            result = model.external_deal_history(
                resolve_window("all"), page=page, page_size=EXTERNAL_DEALS_MAX_PAGE_SIZE,
            )
            batch = list(result.get("items") or [])
            items.extend(batch)
            total = int(result.get("total") or 0)
            if not batch or len(items) >= total:
                break
            page += 1
        return items, f"OK ({len(items)} deals, quality {result.get('quality')})"
    except Exception as exc:  # noqa: BLE001 — the fill history still goes out
        log.exception("deal table export failed")
        return [], f"DEAL_TABLE_UNAVAILABLE ({type(exc).__name__}: {exc})"


MONEY = '"$"#,##0.00;-"$"#,##0.00'
SIGNED_MONEY = '+"$"#,##0.00;-"$"#,##0.00;"$"0.00'
FORMATS = {
    "התחלה": "dd/mm/yyyy hh:mm:ss", "סיום": "dd/mm/yyyy hh:mm:ss",
    "קנייה $": MONEY, "מכירה $": MONEY, "עמלות": MONEY, "תזרים": MONEY,
    "מחיר קנייה": "0.0000", "מחיר מכירה": "0.0000",
    "BTC בכניסה": SIGNED_MONEY, "BTC ביציאה": SIGNED_MONEY,
    "BTC לטובת העסקה בכניסה": SIGNED_MONEY, "BTC לטובת העסקה ביציאה": SIGNED_MONEY,
    "BTC פתיחה (price to beat)": MONEY, "BTC בכניסה (מחיר)": MONEY,
    "BTC ביציאה (מחיר)": MONEY,
}
WIDTHS = {"#": 7, "התחלה": 20, "סיום": 20, "שוק": 44, "טוקן": 8, "פעולות": 8,
          "נזילות כניסה": 16, "סטטוס": 16, "שער BTC": 26, "סטטוס (קוד)": 18,
          "Event Slug": 28}


def add_deal_table_sheet(workbook: Any, items: list[dict[str, Any]]) -> Any:
    """Insert the "עסקאות" sheet first and make it the active sheet."""
    sheet = workbook.create_sheet(SHEET_TITLE, 0)
    sheet.sheet_view.rightToLeft = True
    sheet.append(list(COLUMNS))
    for index, cell in enumerate(sheet[1], start=1):
        extra = index > len(TABLE_COLUMNS)
        cell.fill = PatternFill("solid", fgColor="5B6B82" if extra else "1F3864")
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for item in items:
        row = table_row(item)
        sheet.append([row[column][0] for column in COLUMNS])
        for index, column in enumerate(COLUMNS, start=1):
            tone = row[column][1]
            if tone:
                cell = sheet.cell(row=sheet.max_row, column=index)
                cell.font = Font(color=GREEN if tone == "positive" else RED,
                                 bold=column == "תוצאת שוק")
    for index, column in enumerate(COLUMNS, start=1):
        letter = get_column_letter(index)
        sheet.column_dimensions[letter].width = WIDTHS.get(column, 14)
        if column in FORMATS:
            for cell in sheet[letter][1:]:
                cell.number_format = FORMATS[column]
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    workbook.active = 0
    return sheet
