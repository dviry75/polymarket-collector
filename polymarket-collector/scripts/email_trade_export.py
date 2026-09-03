#!/usr/bin/env python3
"""Resource-capped worker: export the FULL Polymarket trade history and email it.

Triggered by ``polymarket-trade-export.path`` when the dashboard drops a request
file into ``/opt/polymarket-btc-live/export-requests/``. Runs in its own systemd
slice with ``CPUQuota``/``MemoryMax``/``Nice=19`` so it cannot starve the trader;
latency does not matter (minutes is fine).

- The Excel file is produced by ``tools/export_trade_history_simple.py`` **unchanged**
  (single "Trade History" sheet, one row per fill, chronological, freeze + filters,
  MAKER perspective reconstructed from ``maker_orders``). No date filter -- full history.
- SMTP config comes from ``/etc/polymarket-live/smtp.env`` (injected by systemd);
  it is never exposed to the public dashboard process.
- Recipients are restricted to the configured report addresses
  (``HOURLY_REPORT_RECIPIENTS``); the worker also republishes that list to
  ``export-recipients.json`` for the dashboard dropdown.

Usage:
    python -m scripts.email_trade_export --drain /opt/polymarket-btc-live/export-requests
    python -m scripts.email_trade_export --publish-recipients
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live.reporting import SMTPSettings  # noqa: E402
from live.trade_export_queue import RECIPIENTS_FILE, REQUEST_DIR  # noqa: E402
from tools.export_trade_history_simple import (  # noqa: E402
    assert_no_secrets,
    build_rows,
    fetch_clob_trades,
    fetch_data_trades,
    write_workbook,
)

OUTPUT_DIR = Path("/opt/polymarket-btc-live/output")
XLSX_MIME = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")

log = logging.getLogger("email_trade_export")


def _be_nice() -> None:
    try:
        os.nice(19)
    except OSError:
        pass


def resolve_config() -> dict[str, str]:
    """Build the Polymarket config from the environment only.

    systemd injects trader.env via ``EnvironmentFile=`` (read as root), so the
    values are already in ``os.environ``; unlike the standalone tool we must NOT
    try to open the root-only file ourselves.
    """
    def pick(name: str, default: str = "") -> str:
        return os.environ.get(name) or default

    config = {
        "api_key": pick("POLYMARKET_API_KEY"),
        "api_secret": pick("POLYMARKET_API_SECRET"),
        "api_passphrase": pick("POLYMARKET_API_PASSPHRASE"),
        "signer_address": pick("POLYMARKET_SIGNER_ADDRESS"),
        "proxy_wallet": pick("POLYMARKET_FUNDER_ADDRESS", pick("POLYMARKET_PROFILE_ADDRESS")),
        "clob_host": pick("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").rstrip("/"),
        "data_host": pick("POLYMARKET_DATA_API_HOST", "https://data-api.polymarket.com").rstrip("/"),
    }
    missing = [k for k, v in config.items() if not v]
    if missing:
        raise RuntimeError(f"missing Polymarket config from environment: {', '.join(missing)}")
    return config


def publish_recipients() -> list[str]:
    recipients = list(SMTPSettings.from_env().recipients)
    try:
        RECIPIENTS_FILE.write_text(
            json.dumps({"recipients": recipients, "updated_at": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
        log.info("published %d recipient(s) to %s", len(recipients), RECIPIENTS_FILE)
    except OSError as exc:
        log.warning("could not publish recipients file: %s", exc)
    return recipients


def build_workbook(config: dict[str, str]) -> Path:
    clob = fetch_clob_trades(config)
    data = fetch_data_trades(config)
    log.info("fetched CLOB fills=%d data-api rows=%d", len(clob), len(data))
    rows = build_rows(clob, data, config["proxy_wallet"].lower())
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"polymarket_trade_history_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.xlsx"
    write_workbook(rows, path)
    assert_no_secrets(path, config)
    log.info("wrote %s (%d fills)", path, len(rows))
    return path


def send_mail(settings: SMTPSettings, recipient: str, xlsx: Path, fill_count_hint: str) -> None:
    if not settings.sender:
        raise RuntimeError("SMTP_FROM is not configured")
    msg = EmailMessage()
    msg["From"] = settings.sender
    msg["To"] = recipient
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg["Subject"] = f"Polymarket trade history — full export ({stamp})"
    msg["Message-ID"] = f"<trade-export-{int(datetime.now(timezone.utc).timestamp())}@polymarket-live>"
    body = (
        "Attached: the complete Polymarket fill history for the proxy wallet, "
        "one row per fill, no date filter.\n"
        f"Rows: {fill_count_hint}\nGenerated: {stamp}\n"
    )
    msg.set_content(body)
    msg.add_attachment(xlsx.read_bytes(), maintype=XLSX_MIME[0], subtype=XLSX_MIME[1], filename=xlsx.name)
    with smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout_seconds) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.send_message(msg)
    log.info("emailed %s -> %s", xlsx.name, recipient)


def drain(directory: Path) -> int:
    settings = SMTPSettings.from_env()
    allowed = set(settings.recipients)
    done_dir = directory / "done"
    failed_dir = directory / "failed"
    requests = sorted(p for p in directory.glob("*.json") if p.is_file())
    if not requests:
        log.info("no pending export requests")
        return 0

    try:
        config = resolve_config()
    except RuntimeError as exc:
        # Move everything to failed/ so the .path unit stops re-triggering a
        # broken deploy in a tight loop.
        log.error("cannot start export: %s — parking %d request(s) in failed/", exc, len(requests))
        failed_dir.mkdir(parents=True, exist_ok=True)
        for request_path in requests:
            try:
                request_path.replace(failed_dir / request_path.name)
            except OSError:
                pass
        return 1

    workbook: Path | None = None
    exit_code = 0
    for request_path in requests:
        try:
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            recipient = str(payload.get("recipient", "")).strip()
            if recipient not in allowed:
                raise RuntimeError(f"recipient {recipient!r} is not an allowed report address")
            if workbook is None:  # build once, reuse for all pending requests
                workbook = build_workbook(config)
            send_mail(settings, recipient, workbook, fill_count_hint=str(payload.get("id", "")))
            done_dir.mkdir(parents=True, exist_ok=True)
            request_path.replace(done_dir / request_path.name)
        except Exception as exc:  # noqa: BLE001 -- isolate per request
            log.exception("export request %s failed: %s", request_path.name, exc)
            failed_dir.mkdir(parents=True, exist_ok=True)
            try:
                request_path.replace(failed_dir / request_path.name)
            except OSError:
                pass
            exit_code = 1
    return exit_code


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s email_trade_export %(message)s",
    )
    _be_nice()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drain", nargs="?", const=str(REQUEST_DIR), default=None,
                        help="process every *.json request in this directory (default queue dir)")
    parser.add_argument("--publish-recipients", action="store_true",
                        help="(re)write the recipient allow-list file and exit")
    args = parser.parse_args()

    publish_recipients()
    if args.publish_recipients and args.drain is None:
        return 0
    return drain(Path(args.drain or REQUEST_DIR))


if __name__ == "__main__":
    raise SystemExit(main())
