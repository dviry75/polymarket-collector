from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config
from src.csv_storage import HEADERS, ensure_csv_file, upsert_event_row
from src.polymarket import utc_now_iso


def main() -> None:
    config = load_config()
    csv_path = ensure_csv_file(config.csv_events_path, HEADERS)

    timestamp = utc_now_iso()
    row = {header: "" for header in HEADERS}
    row.update(
        {
            "local_event_id": "csv_local_test",
            "condition_id": "csv_local_test_condition",
            "discovered_at": timestamp,
            "last_seen_at": timestamp,
            "status": "csv_write_ok",
            "notes": "Local CSV write test row.",
        }
    )

    action = upsert_event_row(csv_path, row, HEADERS)
    print(f"CSV write OK. Test row {action}: {csv_path}")


if __name__ == "__main__":
    main()
