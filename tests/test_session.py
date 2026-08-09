import errno
import json
import os
import socket
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from cla.session import (
    _acquire_windows_lock,
    clear_session,
    playback_launch_lock,
    publish_session,
    read_session,
    send_command,
)
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


def test_playback_launch_lock_serializes_concurrent_launches(
    session_file: Path,
) -> None:
    first_acquired = threading.Event()
    release_first = threading.Event()
    second_attempted = threading.Event()
    second_acquired = threading.Event()

    def hold_first_lock() -> None:
        with playback_launch_lock():
            first_acquired.set()
            assert release_first.wait(timeout=2)

    def acquire_second_lock() -> None:
        assert first_acquired.wait(timeout=2)
        second_attempted.set()
        with playback_launch_lock():
            second_acquired.set()

    first = threading.Thread(target=hold_first_lock)
    second = threading.Thread(target=acquire_second_lock)
    first.start()
    second.start()
    assert second_attempted.wait(timeout=2)
    assert not second_acquired.wait(timeout=0.05)

    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_acquired.is_set()


def test_windows_launch_lock_retries_beyond_crt_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "launch.lock"
    lock_path.write_bytes(b"\0")

    class Msvcrt:
        LK_NBLCK = 2

        def __init__(self) -> None:
            self.attempts = 0

        def locking(self, fileno: int, mode: int, size: int) -> None:
            assert fileno >= 0
            assert mode == self.LK_NBLCK
            assert size == 1
            self.attempts += 1
            if self.attempts <= 12:
                raise PermissionError(errno.EACCES, "lock is held")

    msvcrt = Msvcrt()
    sleep = Mock()
    monkeypatch.setattr("cla.session.time.sleep", sleep)

    with lock_path.open("r+b") as lock_file:
        _acquire_windows_lock(lock_file, msvcrt)

    assert msvcrt.attempts == 13
    assert sleep.call_count == 12


def test_windows_launch_lock_surfaces_non_contention_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "launch.lock"
    lock_path.write_bytes(b"\0")
    msvcrt = Mock(LK_NBLCK=2)
    msvcrt.locking.side_effect = OSError(errno.EBADF, "invalid handle")
    sleep = Mock()
    monkeypatch.setattr("cla.session.time.sleep", sleep)

    with lock_path.open("r+b") as lock_file:
        with pytest.raises(OSError, match="invalid handle"):
            _acquire_windows_lock(lock_file, msvcrt)

    sleep.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows file-locking regression")
def test_waiting_launch_does_not_read_the_locked_region(
    session_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open = Path.open

    class UnreadableLockFile:
        def __init__(self, lock_file):
            self.lock_file = lock_file

        def __getattr__(self, name):
            return getattr(self.lock_file, name)

        def __enter__(self):
            self.lock_file.__enter__()
            return self

        def __exit__(self, *args):
            return self.lock_file.__exit__(*args)

        def read(self, *args, **kwargs):
            raise PermissionError("locked byte cannot be read")

    def unreadable_open(path, *args, **kwargs):
        return UnreadableLockFile(original_open(path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", unreadable_open)

    with playback_launch_lock():
        pass


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


def test_response_timeout_preserves_a_live_session(
    session_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_session(43210, "secret")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.recv.side_effect = socket.timeout
    monkeypatch.setattr(
        "cla.session.socket.create_connection", Mock(return_value=connection)
    )

    response = send_command("pause")

    assert not response.ok
    assert response.message == "playback control timed out; session may still be active"
    assert session_file.exists()


def test_malformed_session_is_treated_as_inactive(
    session_file: Path,
) -> None:
    session_file.write_text(json.dumps({"version": 999}), encoding="utf-8")

    response = send_command("play")

    assert not response.ok
    assert not session_file.exists()


def test_worker_serves_controls_over_loopback_and_cleans_up(
    session_file: Path, tmp_path: Path
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
    startup_status = tmp_path / "startup.json"
    worker = threading.Thread(
        target=lambda: result.append(_serve(controller, startup_status))
    )
    worker.start()
    deadline = time.monotonic() + 2
    while read_session() is None and time.monotonic() < deadline:
        time.sleep(0.01)

    startup = json.loads(startup_status.read_text(encoding="utf-8"))
    assert startup["ok"] is True
    assert read_session() is not None
    assert send_command("pause").ok
    assert controller.paused
    assert send_command("_shutdown").ok
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [0]
    assert not session_file.exists()


def test_ffplay_start_failure_is_published_to_parent(
    session_file: Path, tmp_path: Path
) -> None:
    def fail_to_start(*args, **kwargs):
        raise OSError("cannot execute")

    controller = PlaybackController(
        "/tools/ffplay",
        [Track(Path("song.mp3"), track=1, disc=1, duration=60.0)],
        popen=fail_to_start,
    )
    startup_status = tmp_path / "startup.json"

    assert _serve(controller, startup_status) == 1

    startup = json.loads(startup_status.read_text(encoding="utf-8"))
    assert startup["ok"] is False
    assert "cannot execute" in startup["error"]
    assert not session_file.exists()


def test_immediate_ffplay_exit_is_published_as_startup_failure(
    session_file: Path, tmp_path: Path
) -> None:
    process = Mock()
    process.poll.return_value = 1
    controller = PlaybackController(
        "/tools/ffplay",
        [Track(Path("song.mp3"), track=1, disc=1, duration=60.0)],
        popen=Mock(return_value=process),
    )
    startup_status = tmp_path / "startup.json"

    assert _serve(controller, startup_status) == 1

    startup = json.loads(startup_status.read_text(encoding="utf-8"))
    assert startup["ok"] is False
    assert startup["error"] == "ffplay exited with status 1 during startup"
    assert not session_file.exists()


def test_later_control_works_after_a_timed_out_slow_command(
    session_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowProcess:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def wait(self, timeout=None):
            time.sleep(0.05)
            self.returncode = -15
            return self.returncode

        def kill(self):
            self.returncode = -9

    controller = PlaybackController(
        "/tools/ffplay",
        [Track(Path("song.mp3"), track=1, disc=1, duration=60.0)],
        popen=lambda *args, **kwargs: SlowProcess(),
    )
    result = []
    worker = threading.Thread(target=lambda: result.append(_serve(controller)))
    worker.start()
    deadline = time.monotonic() + 2
    while read_session() is None and time.monotonic() < deadline:
        time.sleep(0.01)

    monkeypatch.setattr("cla.session.CONTROL_TIMEOUT_SECONDS", 0.01)
    timed_out = send_command("pause")
    assert not timed_out.ok
    assert session_file.exists()

    time.sleep(0.08)
    monkeypatch.setattr("cla.session.CONTROL_TIMEOUT_SECONDS", 0.2)
    assert send_command("play").ok
    assert send_command("_shutdown").ok
    worker.join(timeout=2)

    assert result == [0]
