#!/usr/bin/env python3
"""Send the daily 20:00 (Israel) P&L email once per day. Observability only."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from live.config import LiveConfig
from live.daily_report import DailyReportService, report_window
from live.trade_export_queue import RECIPIENTS_FILE


def _publish_recipients(recipients: tuple[str, ...]) -> None:
    """Keep the dashboard's address list (notification settings, XL export)
    in step with HOURLY_REPORT_RECIPIENTS without needing an export run."""
    try:
        RECIPIENTS_FILE.write_text(json.dumps({
            "recipients": list(recipients),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }), encoding="utf-8")
    except OSError as exc:
        print(f"daily-report: could not publish recipients file: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily P&L email (20:00 -> 20:00 Israel)")
    parser.add_argument("--no-send", action="store_true",
                        help="Print the email that would be sent; send and record nothing")
    args = parser.parse_args()
    service = DailyReportService(Path(LiveConfig.from_env().live_db_path))
    _publish_recipients(service.email.settings.recipients)
    if args.no_send:
        report_date, start, end = report_window(datetime.now(timezone.utc))
        subject, text, _body = service.render(service.build(report_date, start, end))
        print(subject + "\n\n" + text)
        return
    print(service.tick())


if __name__ == "__main__":
    main()
