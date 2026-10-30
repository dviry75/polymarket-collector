"""Explicit operator command: private prompts, exclusive 0600 DEMO env creation.
Never reads/copies LIVE credentials or accepts passwords through command arguments.
"""
import argparse
import getpass
import hmac
import json
import os
from pathlib import Path
import secrets
import sys

from argon2 import PasswordHasher


def write_environment(path,username,password):
    if not username.strip() or len(username)>120 or any(c in username for c in '\r\n\0'):
        raise ValueError("Invalid username")
    if not password or len(password)>1024:
        raise ValueError("Password must contain 1..1024 characters")
    # New hash of the same operator-entered password is preferable to accessing LIVE.
    values={"DEMO_DATA_DIR":"/var/lib/polymarket-demo-v2",
        "DEMO_DB_PATH":"/var/lib/polymarket-demo-v2/demo_v2.sqlite3",
        "DEMO_AUTH_USERNAME":username,"DEMO_AUTH_PASSWORD_HASH":PasswordHasher().hash(password),
        "DEMO_SESSION_SECRET":secrets.token_urlsafe(48),"DEMO_PUBLIC_DATA_ENABLED":"true",
        "DEMO_COOKIE_SECURE":"true","DEMO_SESSION_TTL_SECONDS":"86400",
        "DEMO_OBSERVATION_MAX_AGE_SECONDS":"5","DEMO_DECISION_QUEUE_CAPACITY":"1024",
        "DEMO_TELEMETRY_CAPACITY":"64","DEMO_RECOVERY_STABILITY_SECONDS":"2"}
    # systemd EnvironmentFile treats $ literally; do not source this file in a shell.
    content=''.join(key+'='+json.dumps(value,ensure_ascii=False)+'\n' for key,value in values.items())
    fd=os.open(Path(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not sys.stdin.isatty():
        raise SystemExit("Use an interactive terminal; no piped password input")
    username=input('DEMO username: ')
    password=getpass.getpass('DEMO password (may match LIVE; never read LIVE env): ')
    repeated=getpass.getpass('Confirm DEMO password: ')
    if not hmac.compare_digest(password.encode(),repeated.encode()):
        raise SystemExit('Passwords differ; no file created')
    write_environment(args.output,username,password)
    print('Private DEMO configuration created; values are not displayed')


if __name__=='__main__':
    main()
