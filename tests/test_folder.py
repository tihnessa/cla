import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from cla.cli import (
    ProbeResult,
    _directory_candidates,
    _start_worker,
    _write_manifest,
    main,
)
from cla.worker import Track, _order_tracks, _play_and_wait, worker_main


def _install_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = {"ffplay": "/tools/ffplay", "ffprobe": "/tools/ffprobe"}
    monkeypatch.setattr("cla.cli.shutil.which", tools.get)


def _manifest(path: Path, files: list[Path]) -> Path:
    path.write_text(
        json.dumps(
            {
                "files": [str(file) for file in files],
                "ffplay": "/tools/ffplay",
                "ffprobe": "/tools/ffprobe",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_directory_candidates_are_filtered_non_recursive_and_naturally_sorted(
    tmp_path: Path,
) -> None:
    for name in ["track10.MP3", "track2.mp3", "cover.jpg", "notes.txt"]:
        (tmp_path / name).touch()
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "track1.mp3").touch()

    candidates, error = _directory_candidates(tmp_path)

    assert error is None
    assert [path.name for path in candidates] == ["track2.mp3", "track10.MP3"]


def test_directory_candidates_include_the_complete_case_insensitive_allowlist(
    tmp_path: Path,
) -> None:
    names = ["a.WAV", "b.Mp3", "c.FLAC", "d.OgG", "e.AAC", "f.M4A"]
    for name in [*names, "ignored.wave", "ignored.opus"]:
        (tmp_path / name).touch()

    candidates, error = _directory_candidates(tmp_path)

    assert error is None
    assert [path.name for path in candidates] == names


def test_directory_with_no_matching_files_is_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "cover.jpg").touch()

    assert main([str(tmp_path)]) == 1
    assert "no supported audio files" in capsys.readouterr().err


def test_unreadable_directory_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(Path, "iterdir", Mock(side_effect=PermissionError))

    assert main([str(tmp_path)]) == 1
    assert "not a readable directory" in capsys.readouterr().err


def test_folder_request_writes_snapshot_and_starts_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "track2.mp3").touch()
    (tmp_path / "track10.mp3").touch()
    manifest = tmp_path / "manifest.json"
    write_manifest = Mock(return_value=manifest)
    start_worker = Mock(return_value=None)
    _install_tools(monkeypatch)
    monkeypatch.setattr("cla.cli._write_manifest", write_manifest)
    monkeypatch.setattr("cla.cli._start_worker", start_worker)

    assert main([str(tmp_path)]) == 0
    assert capsys.readouterr() == ("", "")
    files = write_manifest.call_args.args[0]
    assert [path.name for path in files] == ["track2.mp3", "track10.mp3"]
    assert write_manifest.call_args.args[1:] == ("/tools/ffprobe", "/tools/ffplay")
    start_worker.assert_called_once_with(manifest)


def test_folder_request_removes_manifest_when_worker_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "track.mp3").touch()
    manifest = tmp_path / "manifest.json"
    manifest.touch()
    _install_tools(monkeypatch)
    monkeypatch.setattr("cla.cli._write_manifest", Mock(return_value=manifest))
    monkeypatch.setattr("cla.cli._start_worker", Mock(return_value="cannot execute"))

    assert main([str(tmp_path)]) == 1
    assert not manifest.exists()
    assert "cannot execute" in capsys.readouterr().err


def test_manifest_preserves_unicode_paths(tmp_path: Path) -> None:
    files = [tmp_path / "sp ace-音声.mp3"]

    manifest = _write_manifest(files, "/tools/ffprobe", "/tools/ffplay")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    finally:
        manifest.unlink()

    assert data == {
        "files": [str(files[0])],
        "ffprobe": "/tools/ffprobe",
        "ffplay": "/tools/ffplay",
    }


def test_worker_is_started_with_platform_background_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "manifest.json"
    _manifest(manifest, [tmp_path / "track.mp3"])

    class Process:
        pid = 1234

        def poll(self):
            return None

    def popen(command, **options):
        status = Path(command[-1])
        status.write_text(
            json.dumps({"pid": 1234, "ok": True, "error": None}),
            encoding="utf-8",
        )
        return Process()

    player = Mock(side_effect=popen)
    monkeypatch.setattr("cla.cli.subprocess.Popen", player)

    assert _start_worker(manifest) is None

    expected_options = {
        "close_fds": True,
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
    }
    if os.name == "nt":
        expected_options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        expected_options["start_new_session"] = True
    player.assert_called_once_with(
        [
            os.sys.executable,
            "-m",
            "cla.worker",
            str(manifest),
            player.call_args.args[0][-1],
        ],
        **expected_options,
    )


def test_start_worker_reports_worker_startup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path / "manifest.json", [tmp_path / "track.mp3"])

    class Process:
        pid = 4321

        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return -15

        def kill(self):
            raise AssertionError("terminate should be sufficient")

    process = Process()

    def popen(command, **options):
        Path(command[-1]).write_text(
            json.dumps({"pid": 4321, "ok": False, "error": "invalid manifest"}),
            encoding="utf-8",
        )
        return process

    monkeypatch.setattr("cla.cli.subprocess.Popen", popen)

    assert _start_worker(manifest) == "invalid manifest"
    assert process.terminated
    assert not manifest.exists()


def test_start_worker_ignores_status_for_another_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path / "manifest.json", [tmp_path / "track.mp3"])

    class Process:
        pid = 4321

        def poll(self):
            return 1

    def popen(command, **options):
        Path(command[-1]).write_text(
            json.dumps({"pid": 9999, "ok": True, "error": None}),
            encoding="utf-8",
        )
        return Process()

    monkeypatch.setattr("cla.cli.subprocess.Popen", popen)

    error = _start_worker(manifest)

    assert error is not None
    assert "exited" in error


def test_start_worker_rejects_success_from_worker_that_already_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path / "manifest.json", [tmp_path / "track.mp3"])

    class Process:
        pid = 4321

        def poll(self):
            return 1

    def popen(command, **options):
        Path(command[-1]).write_text(
            json.dumps({"pid": 4321, "ok": True, "error": None}),
            encoding="utf-8",
        )
        return Process()

    monkeypatch.setattr("cla.cli.subprocess.Popen", popen)

    assert _start_worker(manifest) == (
        "playback worker exited with status 1 during startup"
    )
    assert not manifest.exists()


def test_metadata_order_uses_disc_track_and_natural_tiebreaker() -> None:
    tracks = [
        Track(Path("z10.mp3"), track=2, disc=1),
        Track(Path("z2.mp3"), track=2, disc=1),
        Track(Path("first.mp3"), track=1, disc=1),
        Track(Path("disc2.mp3"), track=1, disc=2),
    ]

    assert [track.path.name for track in _order_tracks(tracks)] == [
        "first.mp3",
        "z2.mp3",
        "z10.mp3",
        "disc2.mp3",
    ]


def test_missing_track_metadata_uses_natural_order_for_every_file() -> None:
    tracks = [
        Track(Path("10.mp3"), track=1, disc=1),
        Track(Path("2.mp3"), track=None, disc=None),
        Track(Path("1.mp3"), track=9, disc=1),
    ]

    assert [track.path.name for track in _order_tracks(tracks)] == [
        "1.mp3",
        "2.mp3",
        "10.mp3",
    ]


def test_worker_warns_and_passes_all_playable_tracks_to_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = tmp_path / "1-bad.mp3"
    fails = tmp_path / "2-fails.mp3"
    good = tmp_path / "3-good.mp3"
    for path in [bad, fails, good]:
        path.touch()
    manifest = _manifest(tmp_path / "manifest.json", [bad, fails, good])
    probe = Mock(
        side_effect=[
            ProbeResult(error="invalid data"),
            ProbeResult(track=None, disc=None, duration=20.0),
            ProbeResult(track=None, disc=None, duration=30.0),
        ]
    )
    serve = Mock(return_value=0)
    monkeypatch.setattr("cla.worker._probe_audio", probe)
    monkeypatch.setattr("cla.worker._serve", serve)
    startup_status = tmp_path / "startup.json"

    assert worker_main([str(manifest), str(startup_status)]) == 0
    assert not manifest.exists()
    controller = serve.call_args.args[0]
    assert [track.path for track in controller.tracks] == [fails, good]
    assert serve.call_args.args[1] == startup_status
    errors = capsys.readouterr().err
    assert str(bad) in errors and "invalid data" in errors


def test_worker_warns_when_no_candidate_is_playable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = tmp_path / "bad.mp3"
    bad.touch()
    manifest = _manifest(tmp_path / "manifest.json", [bad])
    monkeypatch.setattr(
        "cla.worker._probe_audio", Mock(return_value=ProbeResult(error="invalid"))
    )
    startup_status = tmp_path / "startup.json"

    assert worker_main([str(manifest), str(startup_status)]) == 1
    assert "no playable audio files" in capsys.readouterr().err
    assert json.loads(startup_status.read_text(encoding="utf-8"))["ok"] is False


def test_worker_reports_invalid_manifest_to_parent(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("not json", encoding="utf-8")
    startup_status = tmp_path / "startup.json"

    assert worker_main([str(manifest), str(startup_status)]) == 1

    status = json.loads(startup_status.read_text(encoding="utf-8"))
    assert status["pid"] == os.getpid()
    assert status["ok"] is False
    assert "manifest" in status["error"]


def test_start_worker_timeout_terminates_child_and_removes_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path / "manifest.json", [tmp_path / "track.mp3"])

    class Process:
        pid = 1234

        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return -15

        def kill(self):
            raise AssertionError("terminate should be sufficient")

    process = Process()
    monkeypatch.setattr("cla.cli.subprocess.Popen", Mock(return_value=process))
    monkeypatch.setattr("cla.cli.time.monotonic", Mock(side_effect=[0.0, 100.0]))

    assert _start_worker(manifest) == "playback worker timed out during startup"
    assert process.terminated
    assert not manifest.exists()


def test_play_and_wait_runs_ffplay_synchronously(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_file = tmp_path / "sample.mp3"
    process = Mock()
    process.wait.return_value = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr("cla.worker.subprocess.Popen", popen)

    assert _play_and_wait("/tools/ffplay", audio_file) is None
    process.wait.assert_called_once_with()
    popen.assert_called_once_with(
        [
            "/tools/ffplay",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-nodisp",
            "-autoexit",
            str(audio_file),
        ],
        close_fds=True,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
