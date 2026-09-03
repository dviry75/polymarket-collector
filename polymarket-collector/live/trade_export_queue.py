"""Tiny file-backed queue for on-demand 'email the full trade history' requests.

The dashboard (public-facing, no SMTP creds) only drops a request file here. A
separate, resource-capped systemd unit (`polymarket-trade-export`) drains the
queue, runs the export, and sends the mail. The recipient allow-list is published
by that unit to ``RECIPIENTS_FILE`` so the dashboard never needs SMTP config.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

REQUEST_DIR = Path("/opt/polymarket-btc-live/export-requests")
RECIPIENTS_FILE = Path("/opt/polymarket-btc-live/export-recipients.json")

_ID_RE = re.compile(r"^[0-9A-Za-z._-]{1,64}$")


class ExportQueueError(RuntimeError):
    pass


def list_recipients() -> list[str]:
    """The addresses the export job is allowed to mail to (report recipients)."""
    try:
        data = json.loads(RECIPIENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    values = data.get("recipients") if isinstance(data, dict) else None
    if not isinstance(values, list):
        return []
    return [str(v).strip() for v in values if isinstance(v, str) and v.strip()]


def enqueue(recipient: str) -> dict[str, str]:
    """Validate the recipient against the allow-list and drop a request file."""
    allowed = list_recipients()
    if not allowed:
        raise ExportQueueError("export recipients are not configured on this host")
    if recipient not in allowed:
        raise ExportQueueError("recipient is not one of the configured report addresses")

    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    request_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
    if not _ID_RE.match(request_id):  # defensive; the format above always matches
        raise ExportQueueError("could not build a safe request id")
    payload = {
        "id": request_id,
        "recipient": recipient,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = REQUEST_DIR / f".{request_id}.json.tmp"
    final = REQUEST_DIR / f"{request_id}.json"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(final)  # atomic within the dir
    return {"status": "queued", "id": request_id}
