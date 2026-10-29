from contextlib import AbstractContextManager
import sqlite3
from typing import Protocol


class UnitOfWork(Protocol):
    def transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        """Future atomic DEMO counter/position/ledger writes; never LIVE writes."""
        ...


class SessionStateStore(Protocol):
    """Future DEMO-owned revocation state, independent of credential validation."""

    def current_version(self) -> int: ...

    def revoke_all(self) -> int: ...
