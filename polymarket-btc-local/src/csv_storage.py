import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook


HEADERS = [
    "local_event_id",
    "polymarket_event_id",
    "polymarket_market_id",
    "condition_id",
    "event_slug",
    "market_slug",
    "title",
    "question",
    "event_url",
    "start_time",
    "end_time",
    "yes_token_id",
    "no_token_id",
    "outcomes",
    "outcome_prices",
    "active",
    "closed",
    "enable_order_book",
    "created_at_polymarket",
    "discovered_at",
    "last_seen_at",
    "status",
    "notes",
]

UPSERT_UPDATE_FIELDS = ["last_seen_at", "active", "closed", "status", "notes"]

EVENT_LOG_HEADERS = [
    "sampled_at",
    "event_slug",
    "event_id",
    "market_id",
    "condition_id",
    "start_time",
    "end_time",
    "up_token_id",
    "down_token_id",
    "up_best_ask",
    "up_best_bid",
    "up_midpoint",
    "up_spread",
    "up_last_trade_price",
    "up_orderbook_timestamp",
    "down_best_ask",
    "down_best_bid",
    "down_midpoint",
    "down_spread",
    "down_last_trade_price",
    "down_orderbook_timestamp",
    "up_trades_count_window",
    "down_trades_count_window",
    "up_volume_shares_window",
    "down_volume_shares_window",
    "up_volume_usdc_window",
    "down_volume_usdc_window",
    "total_trades_count_window",
    "total_volume_usdc_window",
    "status",
    "error",
]


def ensure_csv_file(csv_path: str | Path, headers: list[str]) -> Path:
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists() or path.stat().st_size == 0:
        write_rows(path, [], headers)
        return path

    rows = read_rows(path)
    write_rows(path, rows, headers)
    return path


def read_rows(csv_path: str | Path) -> list[dict[str, str]]:
    path = Path(csv_path)
    if not path.exists() or path.stat().st_size == 0:
        return []

    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        return [dict(row) for row in reader]


def write_rows(csv_path: str | Path, rows: list[dict[str, Any]], headers: list[str]) -> None:
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({header: _cell_value(row.get(header, "")) for header in headers})


def append_row(csv_path: str | Path, row_dict: dict[str, Any], headers: list[str]) -> None:
    path = ensure_csv_file(csv_path, headers)

    with path.open("a", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=headers, extrasaction="ignore")
        writer.writerow({header: _cell_value(row_dict.get(header, "")) for header in headers})


def upsert_event_row(
    csv_path: str | Path,
    row_dict: dict[str, Any],
    headers: list[str],
    key_field: str = "condition_id",
) -> str:
    path = ensure_csv_file(csv_path, headers)
    rows = read_rows(path)
    key_value = str(row_dict.get(key_field, "")).strip()
    if not key_value:
        raise ValueError(f"Cannot upsert row without {key_field}.")

    for row in rows:
        if str(row.get(key_field, "")).strip() == key_value:
            for field in UPSERT_UPDATE_FIELDS:
                row[field] = _cell_value(row_dict.get(field, ""))
            write_rows(path, rows, headers)
            return "updated"

    rows.append({header: _cell_value(row_dict.get(header, "")) for header in headers})
    write_rows(path, rows, headers)
    return "appended"


def _cell_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def export_csv_to_xlsx(csv_path: str | Path, output_dir: str | Path = "output") -> Path:
    rows = read_rows(csv_path)
    headers = EVENT_LOG_HEADERS

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    xlsx_path = output_path / f"polymarket_btc_run_{timestamp}.xlsx"

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "event_logs"
    worksheet.append(headers)

    for row in rows:
        worksheet.append([row.get(header, "") for header in headers])

    workbook.save(xlsx_path)
    return xlsx_path
