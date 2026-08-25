#!/usr/bin/env python3
"""Drain the [CRITICAL ACTION] operator-notification outbox.

Observability/notification only. This process NEVER submits, cancels, pauses,
resumes, or otherwise mutates trading state. Its only writes are the
notification bookkeeping columns on `live_alerts`, performed through
`StrategyRepository.record_alert_notification_result` so every send is
transactional and audited.

Runs as an isolated sidecar (`polymarket-critical-alerts.service`) so the
trading core never has to be restarted or carry SMTP credentials.
"""

from __future__ import annotations

import argparse
import html
from datetime import datetime, timezone

from live.config import LiveConfig
from live.reporting import EmailService, SMTPSettings
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository


def _message_id(alert_id: int) -> str:
    stamp = int(datetime.now(timezone.utc).timestamp())
    return f"<critical-action-{alert_id}-{stamp}@polymarket-live>"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send pending [CRITICAL ACTION] operator notifications"
    )
    parser.add_argument(
        "--no-send", action="store_true",
        help="Show what would be sent without sending or recording anything",
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="Maximum notifications to drain in one tick",
    )
    args = parser.parse_args()

    config = LiveConfig.from_env()
    repo = StrategyRepository(LiveRepository(config.live_db_path))

    pending = repo.critical_email_outbox(limit=args.limit)
    if not pending:
        print("critical-alerts: 0 pending")
        return 0

    if args.no_send:
        for item in pending:
            print(f"WOULD SEND alert_id={item['alert_id']} subject={item['subject']!r}")
        print(f"critical-alerts: {len(pending)} pending (dry run, nothing recorded)")
        return 0

    email = EmailService(SMTPSettings.from_env())
    sent = failed = 0
    for item in pending:
        alert_id = int(item["alert_id"])
        try:
            body = str(item["text"])
            email.send(
                subject=str(item["subject"]),
                text=body,
                html_body="<pre>" + html.escape(body) + "</pre>",
                message_id=_message_id(alert_id),
            )
        except Exception as exc:
            failed += 1
            # Record the failure so attempts increment and the outbox stops
            # retrying after 3 tries instead of looping forever.
            try:
                repo.record_alert_notification_result(
                    alert_id, sent=False,
                    error=f"{type(exc).__name__}: {exc}",
                    actor="critical_alerts",
                )
            except KeyError:
                pass  # alert was resolved/acknowledged between read and write
            print(f"critical-alerts: alert_id={alert_id} FAILED: {type(exc).__name__}")
            continue
        try:
            repo.record_alert_notification_result(
                alert_id, sent=True, actor="critical_alerts",
            )
            sent += 1
            print(f"critical-alerts: alert_id={alert_id} SENT")
        except KeyError:
            print(f"critical-alerts: alert_id={alert_id} sent but no longer OPEN")

    print(f"critical-alerts: sent={sent} failed={failed} pending={len(pending)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
