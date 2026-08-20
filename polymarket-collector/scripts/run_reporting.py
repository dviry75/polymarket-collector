#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from live.config import LiveConfig
from live.reporting import EmailService, ReportingService, SMTPSettings


def main() -> None:
    parser=argparse.ArgumentParser(description="Safe observability-only reporting utility")
    parser.add_argument("action",choices=("migrate","tick","test-email"))
    parser.add_argument("--no-send",action="store_true")
    args=parser.parse_args()
    if args.action=="test-email":
        EmailService(SMTPSettings.from_env()).send_test()
        print("SMTP application test: PASS")
        return
    service=ReportingService(Path(LiveConfig.from_env().live_db_path))
    service.migrate()
    if args.action=="migrate": print("Reporting migration: COMPLETE")
    else: print(service.tick(send=not args.no_send))


if __name__=="__main__": main()
