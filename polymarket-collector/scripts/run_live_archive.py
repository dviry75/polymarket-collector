from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.archive import SnapshotArchiveManager
from live.config import LiveConfig
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository

config = LiveConfig.from_env()
errors = config.validation_errors()
if errors:
    raise SystemExit("invalid safe LIVE configuration")
repo = LiveRepository(config.live_db_path)
repo.migrate(config.live_kill_switch_default)
strategy = StrategyRepository(repo)
strategy.migrate(pause_entries_default=True)
result = SnapshotArchiveManager(config, repo, strategy).run_once()
print(result.status)
raise SystemExit(0 if result.status in {"verified", "no_data"} else 1)
