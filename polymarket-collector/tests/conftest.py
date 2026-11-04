import pytest


@pytest.fixture(autouse=True)
def _isolated_notification_prefs(tmp_path, monkeypatch):
    # Never read or write the production notification-prefs file from tests.
    monkeypatch.setenv("NOTIFICATION_PREFS_FILE", str(tmp_path / "notification-prefs.json"))
