from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_playback_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from reading or replacing a user's real playback session."""
    monkeypatch.setenv("CLA_SESSION_FILE", str(tmp_path / "cla-session.json"))
