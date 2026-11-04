"""Explicit operator command: private prompts, exclusive 0600 DEMO env creation.
Never reads/copies LIVE credentials or accepts passwords through command arguments.
"""
import argparse
import fcntl
import getpass
import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import stat
import tempfile

from argon2 import PasswordHasher
from ..config import parse_additional_users


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


def add_user(path, username, password_hash):
    """Atomically add an account; preserve every existing env value and a private backup.

    Only the generated EnvironmentFile format is accepted. No shell sourcing,
    LIVE lookup, DB change, session-secret rotation or service operation occurs.
    """
    if (not username.strip() or len(username)>120 or any(ord(c)<32 or ord(c)==127 for c in username)
            or not password_hash.startswith('$argon2id$')):
        raise ValueError('Invalid DEMO credentials')
    PasswordHasher().check_needs_rehash(password_hash)
    path=Path(path)
    lock_fd=os.open(path.with_suffix(path.suffix+'.users.lock'),os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(lock_fd,'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        info=path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid()
                or stat.S_IMODE(info.st_mode)!=0o600):
            raise ValueError('DEMO env must be an owned private regular file')
        original=path.read_text()
        lines=original.splitlines(keepends=True)
        relevant={}
        for index,line in enumerate(lines):
            key,separator,value=line.rstrip('\r\n').partition('=')
            if separator and key in ('DEMO_AUTH_USERNAME','DEMO_LOGIN_USERNAME','DEMO_AUTH_USERS_JSON'):
                if key in relevant:
                    raise ValueError('Duplicate DEMO env setting')
                relevant[key]=(index,json.loads(value))
        primary=relevant.get('DEMO_AUTH_USERNAME',relevant.get('DEMO_LOGIN_USERNAME',(None,None)))[1]
        users=dict(parse_additional_users(relevant.get('DEMO_AUTH_USERS_JSON',(None,'{}'))[1]))
        if not primary or username==primary or username in users or len(users)>=20:
            raise ValueError('DEMO username already exists or account limit reached')
        users[username]=password_hash
        line='DEMO_AUTH_USERS_JSON='+json.dumps(json.dumps(users,ensure_ascii=False))+'\n'
        if 'DEMO_AUTH_USERS_JSON' in relevant:
            lines[relevant['DEMO_AUTH_USERS_JSON'][0]]=line
            updated=''.join(lines)
        else:
            updated=original+('' if original.endswith('\n') else '\n')+line
        # Backup remains beside the root-private config, never in a release bundle.
        backup_fd,_=tempfile.mkstemp(prefix=path.name+'.before-user-',dir=path.parent)
        with os.fdopen(backup_fd,'w') as stream:
            stream.write(original);stream.flush();os.fsync(stream.fileno())
        fd,temporary=tempfile.mkstemp(prefix=path.name+'.new-',dir=path.parent)
        try:
            with os.fdopen(fd,'w') as stream:
                os.fchown(stream.fileno(),info.st_uid,info.st_gid)
                stream.write(updated);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
            directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--add-user',help='Add a new username to an existing private DEMO env')
    parser.add_argument('--hash-stdin',action='store_true',help='Read an Argon2id hash, never a password, from stdin')
    args=parser.parse_args()
    if args.add_user:
        if args.hash_stdin:
            password_hash=sys.stdin.readline().rstrip('\r\n')
        else:
            if not sys.stdin.isatty():raise SystemExit('Use an interactive terminal')
            password=getpass.getpass('New DEMO password: ')
            repeated=getpass.getpass('Confirm new DEMO password: ')
            if not password or not hmac.compare_digest(password.encode(),repeated.encode()):
                raise SystemExit('Passwords differ or are empty; no file changed')
            password_hash=PasswordHasher().hash(password)
        try:add_user(args.output,args.add_user,password_hash)
        except Exception:raise SystemExit('DEMO account provisioning failed; private values are not displayed') from None
        print('Additional DEMO account provisioned; restart the guest app to load it')
        return
    if args.hash_stdin:raise SystemExit('--hash-stdin requires --add-user')
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
