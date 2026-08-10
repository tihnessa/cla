import errno
import json
import os
import socket
import stat
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from cla.cli import ProbeResult, _write_manifest
from cla.session import (
    ControlResponse,
    PlaybackStatus,
    QueueSnapshot,
    _acquire_windows_lock,
    _session_path,
    clear_session,
    encode_response,
    playback_launch_lock,
    publish_session,
    read_session,
    send_append,
    send_command,
)
from cla.worker import AppendRequest, PlaybackController, Track, _handle_append, _serve


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode regression")
def test_default_session_state_uses_a_private_runtime_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLA_SESSION_FILE")
    runtime_root = tmp_path / "user-runtime"
    runtime_root.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))

    session_path = _session_path()

    assert session_path.name == "session.json"
    assert session_path.parent.parent == runtime_root
    metadata = session_path.parent.stat()
    assert stat.S_ISDIR(metadata.st_mode)
    assert metadata.st_uid == os.getuid()
    assert stat.S_IMODE(metadata.st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX runtime namespace regression")
def test_default_session_state_falls_back_beneath_the_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLA_SESSION_FILE")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    shared_temporary = tmp_path / "shared"
    shared_temporary.mkdir(mode=0o777)
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    monkeypatch.setattr(
        "cla.session.tempfile.gettempdir", lambda: str(shared_temporary)
    )
    monkeypatch.setattr("cla.session.Path.home", lambda: home)

    session_path = _session_path()

    assert session_path == home / ".cla" / "run" / "session.json"
    assert shared_temporary not in session_path.parents
    assert stat.S_IMODE(session_path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink regression")
def test_launch_lock_does_not_follow_or_chmod_a_symlink(
    session_file: Path, tmp_path: Path
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("untouched", encoding="utf-8")
    victim.chmod(0o755)
    lock_path = session_file.with_name(f"{session_file.name}.launch.lock")
    lock_path.symlink_to(victim)

    with pytest.raises(OSError):
        with playback_launch_lock():
            pass

    assert victim.read_text(encoding="utf-8") == "untouched"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o755


def test_token_safe_cleanup_cannot_delete_a_replacement_session(
    session_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_session(43210, "stale")
    stale_read = threading.Event()
    continue_cleanup = threading.Event()
    replacement_published = threading.Event()

    original_read_session = read_session

    def pause_after_stale_read():
        descriptor = original_read_session()
        if threading.current_thread().name == "stale-cleanup":
            stale_read.set()
            assert continue_cleanup.wait(timeout=2)
        return descriptor

    monkeypatch.setattr("cla.session.read_session", pause_after_stale_read)

    cleanup = threading.Thread(
        target=clear_session, args=("stale",), name="stale-cleanup"
    )

    def publish_replacement() -> None:
        publish_session(43211, "replacement")
        replacement_published.set()

    replacement = threading.Thread(target=publish_replacement)
    cleanup.start()
    assert stale_read.wait(timeout=2)
    replacement.start()
    assert not replacement_published.wait(timeout=0.05)

    continue_cleanup.set()
    cleanup.join(timeout=2)
    replacement.join(timeout=2)

    assert not cleanup.is_alive()
    assert not replacement.is_alive()
    descriptor = original_read_session()
    assert descriptor is not None
    assert descriptor.token == "replacement"


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
    original_fdopen = os.fdopen

    class InaccessibleLockRegion:
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

        def write(self, *args, **kwargs):
            raise PermissionError("locked byte cannot be written")

    def inaccessible_fdopen(*args, **kwargs):
        return InaccessibleLockRegion(original_fdopen(*args, **kwargs))

    monkeypatch.setattr("cla.session.os.fdopen", inaccessible_fdopen)

    with playback_launch_lock():
        pass


def test_stale_session_is_removed_when_connection_fails(
    session_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_session(43210, "secret")
    monkeypatch.setattr(
        "cla.session.socket.create_connection",
        Mock(side_effect=ConnectionRefusedError("refused")),
    )

    response = send_command("pause")

    assert not response.ok
    assert response.message == "no active playback session"
    assert response.unavailable
    assert not session_file.exists()


def test_other_connection_failures_are_reported_and_preserve_the_session(
    session_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_session(43210, "secret")
    monkeypatch.setattr(
        "cla.session.socket.create_connection",
        Mock(side_effect=OSError("socket unavailable")),
    )

    response = send_command("status")

    assert not response.ok
    assert response.message == "could not contact playback session: socket unavailable"
    assert not response.unavailable
    assert session_file.exists()


def test_transport_failures_are_reported_and_preserve_the_session(
    session_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_session(43210, "secret")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.recv.side_effect = ConnectionResetError("reset")
    monkeypatch.setattr(
        "cla.session.socket.create_connection", Mock(return_value=connection)
    )

    response = send_command("status")

    assert not response.ok
    assert response.message == "playback communication failed: reset"
    assert not response.unavailable
    assert session_file.exists()


def test_status_response_round_trip(
    session_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_session(43210, "secret")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.recv.return_value = encode_response(
        ControlResponse(
            True,
            status=PlaybackStatus("Song", elapsed=12.5, duration=90.0),
        )
    )
    monkeypatch.setattr(
        "cla.session.socket.create_connection", Mock(return_value=connection)
    )

    assert send_command("status") == ControlResponse(
        True,
        status=PlaybackStatus("Song", elapsed=12.5, duration=90.0),
    )


def test_queue_response_round_trip(
    session_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_session(43210, "secret")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.recv.return_value = encode_response(
        ControlResponse(
            True,
            queue=QueueSnapshot(("First", "second.mp3"), current_index=2),
        )
    )
    monkeypatch.setattr(
        "cla.session.socket.create_connection", Mock(return_value=connection)
    )

    assert send_command("list") == ControlResponse(
        True,
        queue=QueueSnapshot(("First", "second.mp3"), current_index=2),
    )


def test_queue_response_can_exceed_the_old_fixed_response_limit(
    session_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_session(43210, "secret")
    labels = tuple(f"track-{index:06d}.mp3" for index in range(70_000))
    encoded = encode_response(
        ControlResponse(True, queue=QueueSnapshot(labels, current_index=35_001))
    )
    assert len(encoded) > 1024 * 1024
    chunks = [
        encoded[start : start + 64 * 1024]
        for start in range(0, len(encoded), 64 * 1024)
    ]
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.recv.side_effect = chunks
    monkeypatch.setattr(
        "cla.session.socket.create_connection", Mock(return_value=connection)
    )

    response = send_command("list")

    assert response.queue == QueueSnapshot(labels, current_index=35_001)


@pytest.mark.parametrize(
    "queue",
    [
        {"labels": [], "current_index": 1},
        {"labels": ["one"], "current_index": 0},
        {"labels": ["one"], "current_index": 2},
        {"labels": [""], "current_index": 1},
        {"labels": ["one"], "current_index": True},
    ],
)
def test_malformed_queue_response_is_a_protocol_error(
    queue: object, session_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_session(43210, "secret")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.recv.return_value = (
        json.dumps({"ok": True, "message": None, "queue": queue}).encode() + b"\n"
    )
    monkeypatch.setattr(
        "cla.session.socket.create_connection", Mock(return_value=connection)
    )

    response = send_command("list")

    assert not response.ok
    assert response.message == "invalid control response"


def test_malformed_status_response_is_a_protocol_error(
    session_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_session(43210, "secret")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.recv.return_value = (
        b'{"ok":true,"message":null,"status":'
        b'{"title":"Song","elapsed":"soon","duration":90}}\n'
    )
    monkeypatch.setattr(
        "cla.session.socket.create_connection", Mock(return_value=connection)
    )

    response = send_command("status")

    assert not response.ok
    assert response.message == "invalid control response"
    assert not response.unavailable


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
    while (
        read_session() is None or not startup_status.exists()
    ) and time.monotonic() < deadline:
        time.sleep(0.01)

    startup = json.loads(startup_status.read_text(encoding="utf-8"))
    assert startup["ok"] is True
    assert read_session() is not None
    assert send_command("pause").ok
    assert controller.paused
    paused_offset = controller.offset
    assert send_command("ff20").ok
    assert controller.offset == pytest.approx(paused_offset + 20)
    assert controller.paused
    assert send_command("_shutdown").ok
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [0]
    assert not session_file.exists()


def test_kill_stops_worker_and_cleans_up_session(
    session_file: Path, tmp_path: Path
) -> None:
    process = Mock(returncode=None)
    process.poll.side_effect = lambda: process.returncode
    process.terminate.side_effect = lambda: setattr(process, "returncode", -15)
    process.wait.side_effect = lambda timeout=None: process.returncode
    controller = PlaybackController(
        "/tools/ffplay",
        [
            Track(Path("one.mp3"), track=1, disc=1, duration=60.0),
            Track(Path("two.mp3"), track=2, disc=1, duration=60.0),
        ],
        popen=Mock(return_value=process),
    )
    result = []
    worker = threading.Thread(target=lambda: result.append(_serve(controller)))
    worker.start()
    deadline = time.monotonic() + 2
    while read_session() is None and time.monotonic() < deadline:
        time.sleep(0.01)

    response = send_command("kill")
    worker.join(timeout=2)

    assert response == ControlResponse(True)
    assert not worker.is_alive()
    assert result == [0]
    assert controller.index == 0
    process.terminate.assert_called_once_with()
    assert not session_file.exists()


def test_finished_worker_accepts_an_atomic_append(
    session_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    processes = []

    def popen(*args, **kwargs):
        process = Process()
        processes.append(process)
        return process

    controller = PlaybackController(
        "/tools/ffplay",
        [Track(Path("one.mp3"), track=1, disc=1, duration=1.0)],
        popen=popen,
    )
    worker = threading.Thread(target=lambda: _serve(controller))
    worker.start()
    deadline = time.monotonic() + 2
    while read_session() is None and time.monotonic() < deadline:
        time.sleep(0.01)

    processes[0].returncode = 0
    deadline = time.monotonic() + 2
    while not controller.stopped and time.monotonic() < deadline:
        time.sleep(0.01)
    assert read_session() is not None

    added = tmp_path / "added.mp3"
    added.touch()
    manifest = _write_manifest([added], "/tools/ffprobe", "/tools/ffplay")
    monkeypatch.setattr(
        "cla.worker._probe_audio",
        Mock(return_value=ProbeResult(track=1, duration=2.0)),
    )

    response = send_append(manifest, candidate_count=1)

    assert response.ok
    assert not manifest.exists()
    assert controller.current.path == added
    assert not controller.stopped
    assert len(processes) == 2
    assert send_command("_shutdown").ok
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_unplayable_append_leaves_queue_unchanged(
    session_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = Mock(returncode=None)
    process.poll.side_effect = lambda: process.returncode
    process.terminate.side_effect = lambda: setattr(process, "returncode", -15)
    process.wait.side_effect = lambda timeout=None: process.returncode
    controller = PlaybackController(
        "/tools/ffplay",
        [Track(Path("one.mp3"), track=1, disc=1, duration=60.0)],
        popen=Mock(return_value=process),
    )
    worker = threading.Thread(target=lambda: _serve(controller))
    worker.start()
    deadline = time.monotonic() + 2
    while read_session() is None and time.monotonic() < deadline:
        time.sleep(0.01)

    bad = tmp_path / "bad.mp3"
    bad.touch()
    manifest = _write_manifest([bad], "/tools/ffprobe", "/tools/ffplay")
    monkeypatch.setattr(
        "cla.worker._probe_audio", Mock(return_value=ProbeResult(error="invalid"))
    )

    response = send_append(manifest, candidate_count=1)

    assert not response.ok
    assert "no playable" in (response.message or "")
    assert response.warnings == (f"{bad}: invalid",)
    assert controller.tracks == [Track(Path("one.mp3"), track=1, disc=1, duration=60.0)]
    assert send_command("_shutdown").ok
    worker.join(timeout=2)


def test_append_probes_the_whole_batch_before_mutating_and_keeps_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Track(Path("one.mp3"), track=1, disc=1, duration=60.0)
    controller = PlaybackController("/tools/ffplay", [original], popen=Mock())
    bad = tmp_path / "bad.mp3"
    good = tmp_path / "good.mp3"
    bad.touch()
    good.touch()
    manifest = _write_manifest([bad, good], "/tools/ffprobe", "/tools/ffplay")

    def probe(_ffprobe: str, path: Path) -> ProbeResult:
        assert controller.tracks == [original]
        if path == bad:
            return ProbeResult(error="invalid")
        return ProbeResult(track=2, duration=30.0)

    monkeypatch.setattr("cla.worker._probe_audio", probe)

    response = _handle_append(controller, AppendRequest(manifest))

    assert response.ok
    assert response.warnings == (f"{bad}: invalid",)
    assert [track.path for track in controller.tracks] == [original.path, good]


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
