import json
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from cla.session import clear_session, publish_session, read_session, send_command
from cla.worker import PlaybackController, Track, _serve


@pytest.fixture
def session_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "session.json"
    monkeypatch.setenv("CLA_SESSION_FILE", str(path))
    return path


def test_session_descriptor_round_trip_and_token_safe_cleanup(
    session_file: Path,
) -> None:
    descriptor = publish_session(43210, "secret")

    assert read_session() == descriptor
    clear_session("someone-elses-token")
    assert session_file.exists()
    clear_session("secret")
    assert not session_file.exists()


def test_stale_session_is_removed_when_connection_fails(
    session_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_session(43210, "secret")
    monkeypatch.setattr(
        "cla.session.socket.create_connection", Mock(side_effect=OSError("refused"))
    )

    response = send_command("pause")

    assert not response.ok
    assert response.message == "no active playback session"
    assert not session_file.exists()


def test_malformed_session_is_treated_as_inactive(
    session_file: Path,
) -> None:
    session_file.write_text(json.dumps({"version": 999}), encoding="utf-8")

    response = send_command("play")

    assert not response.ok
    assert not session_file.exists()


def test_worker_serves_controls_over_loopback_and_cleans_up(
    session_file: Path,
) -> None:
    class Process:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    controller = PlaybackController(
        "/tools/ffplay",
        [Track(Path("song.mp3"), track=1, disc=1, duration=60.0)],
        popen=lambda *args, **kwargs: Process(),
    )
    result = []
    worker = threading.Thread(target=lambda: result.append(_serve(controller)))
    worker.start()
    deadline = time.monotonic() + 2
    while read_session() is None and time.monotonic() < deadline:
        time.sleep(0.01)

    assert send_command("pause").ok
    assert controller.paused
    assert send_command("_shutdown").ok
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [0]
    assert not session_file.exists()
