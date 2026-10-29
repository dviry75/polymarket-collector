from contextlib import contextmanager
import sqlite3
from typing import Iterator

from ..config import DemoConfig


class DemoDatabase:
    """Connection lifecycle only. Phase 1 creates no tables or migrations."""

    def __init__(self, config: DemoConfig):
        self.config = config

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # Revalidate at the actual I/O boundary, including symlink/hardlink
        # changes since configuration creation. No implicit connection on import.
        checked = DemoConfig(data_dir=self.config.data_dir, db_path=self.config.db_path)
        checked.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(checked.db_path), timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if any(not row[0].startswith(("demo_v2_", "sqlite_")) for row in tables):
                raise ValueError("database contains tables outside the DEMO V2 namespace")
            yield conn
        finally:
            conn.close()

    def probe(self) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1
