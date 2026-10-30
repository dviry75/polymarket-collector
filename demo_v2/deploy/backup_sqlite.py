"""Explicit operator-only WAL-aware backup. No timers, pruning or source mutation."""
import argparse
import os
from pathlib import Path
import sqlite3


def backup(source,destination):
    source,destination=Path(source).resolve(),Path(destination).resolve()
    if source.name!='demo_v2.sqlite3' or not source.is_file() or source==destination:
        raise ValueError('Source must be an existing dedicated demo_v2.sqlite3')
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as reader:
        tables=[row[0] for row in reader.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if not tables or any(not name.startswith(('demo_v2_','sqlite_')) for name in tables):
            raise ValueError('Source is not a DEMO-only database')
        fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        os.close(fd)
        with sqlite3.connect(destination) as writer:
            reader.backup(writer,pages=256,sleep=0.05)
            writer.execute('PRAGMA journal_mode=DELETE')
            if writer.execute('PRAGMA quick_check').fetchone()[0]!='ok' or writer.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('Backup verification failed; do not use this destination')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args()
    backup(args.source,args.destination)
    print('DEMO backup created and verified; source history retained')


if __name__=='__main__':
    main()
