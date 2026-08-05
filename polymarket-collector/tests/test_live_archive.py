from pathlib import Path
import gzip
import sqlite3
import tempfile
from unittest.mock import patch

from live.archive import SnapshotArchiveManager
from live.backup import LiveBackupManager
from live.config import LiveConfig
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository


class FakeBlob:
    def __init__(self, name, objects):
        self.name = name
        self.objects = objects
        self.generation = 1

    def upload_from_filename(self, path, **kwargs):
        assert kwargs["if_generation_match"] == 0
        self.objects[self.name] = Path(path).read_bytes()

    def upload_from_string(self, value, **kwargs):
        assert kwargs["if_generation_match"] == 0
        self.objects[self.name] = value.encode()

    def download_as_bytes(self, **kwargs):
        return self.objects[self.name]

    def download_as_text(self, **kwargs):
        return self.objects[self.name].decode()


class FakeBucket:
    lifecycle_rules = [{"action": {"type": "Delete"}, "condition": {"age": 365}}]

    def __init__(self):
        self.objects = {}

    def blob(self, name):
        return FakeBlob(name, self.objects)


class FakeStorageClient:
    bucket = FakeBucket()

    def __init__(self, **kwargs):
        pass

    def get_bucket(self, name):
        assert name == "dedicated-live-bucket"
        return self.bucket


def setup(tmp, bucket=""):
    db = Path(tmp) / "live.sqlite3"
    config = LiveConfig(
        live_db_path=str(db), backup_dir=str(Path(tmp) / "backups"),
        archive_bucket=bucket, google_project_id="test-project",
    )
    repo = LiveRepository(db)
    repo.migrate()
    strategy = StrategyRepository(repo)
    strategy.migrate()
    return config, repo, strategy


def add_old_snapshot(repo, identity):
    return repo.store_market_snapshot({
        "condition_id": "condition", "event_id": "event", "asset_id": "token",
        "outcome": "YES", "event_type": "book", "best_bid": "0.70",
        "best_ask": "0.74", "bids": [{"price": "0.70", "size": "1"}],
        "asks": [{"price": "0.74", "size": "1"}],
        "received_at": "2020-01-01T00:00:00+00:00", "message_hash": identity,
    })


def test_archive_missing_bucket_alerts_and_never_deletes():
    with tempfile.TemporaryDirectory() as tmp:
        config, repo, strategy = setup(tmp)
        add_old_snapshot(repo, "one")
        result = SnapshotArchiveManager(config, repo, strategy).run_once()
        assert result.status == "blocked"
        assert len(repo.list_table("live_market_snapshots", 10)) == 1
        assert strategy.active_alerts()[0]["reason_code"] == "ARCHIVE_BUCKET_MISSING"


def test_archive_upload_manifest_readback_checksum_then_delete():
    with tempfile.TemporaryDirectory() as tmp:
        FakeStorageClient.bucket = FakeBucket()
        config, repo, strategy = setup(tmp, "dedicated-live-bucket")
        add_old_snapshot(repo, "one")
        add_old_snapshot(repo, "two")
        with patch("google.cloud.storage.Client", FakeStorageClient):
            result = SnapshotArchiveManager(config, repo, strategy).run_once()
        assert result.status == "verified"
        assert result.row_count == 2 and result.local_rows_deleted == 2
        assert result.readback_verified and len(result.sha256) == 64
        assert repo.list_table("live_market_snapshots", 10) == []
        objects = FakeStorageClient.bucket.objects
        assert any(name.endswith("market_snapshots.ndjson.gz") for name in objects)
        assert any(name.endswith("manifest.json") for name in objects)
        archive = repo.list_table("live_archive_runs", 1)[0]
        assert archive["status"] == "verified" and archive["readback_verified"] == 1


def test_backup_restore_sample_integrity():
    with tempfile.TemporaryDirectory() as tmp:
        config, repo, strategy = setup(tmp)
        strategy.timeline(
            severity="INFO", category="TEST", component="backup", source="test",
            requested_action="BACKUP", reason_code="RESTORE_SAMPLE", result_status="OK",
        )
        result = LiveBackupManager(config, repo).create_backup("test_restore")
        restored = Path(tmp) / "restored.sqlite3"
        with gzip.open(result.path, "rb") as source, restored.open("wb") as target:
            target.write(source.read())
        conn = sqlite3.connect(restored)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT COUNT(*) FROM live_audit_timeline").fetchone()[0] == 1
        finally:
            conn.close()
