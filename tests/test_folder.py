import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from clp.cli import (
    ProbeResult,
    _directory_candidates,
    _start_worker,
    _write_manifest,
    main,
)
from clp.worker import Track, _order_tracks, _play_and_wait, worker_main


def _install_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = {"ffplay": "/tools/ffplay", "ffprobe": "/tools/ffprobe"}
    monkeypatch.setattr("clp.cli.shutil.which", tools.get)


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
    monkeypatch.setattr("clp.cli._write_manifest", write_manifest)
    monkeypatch.setattr("clp.cli._start_worker", start_worker)

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
    monkeypatch.setattr("clp.cli._write_manifest", Mock(return_value=manifest))
    monkeypatch.setattr("clp.cli._start_worker", Mock(return_value="cannot execute"))

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
    player = Mock()
    monkeypatch.setattr("clp.cli.subprocess.Popen", player)

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
        [os.sys.executable, "-m", "clp.worker", str(manifest)],
        **expected_options,
    )


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


def test_worker_warns_and_continues_after_probe_and_playback_failures(
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
            ProbeResult(track=None, disc=None),
            ProbeResult(track=None, disc=None),
        ]
    )
    play = Mock(side_effect=["ffplay exited with status 1", None])
    monkeypatch.setattr("clp.worker._probe_audio", probe)
    monkeypatch.setattr("clp.worker._play_and_wait", play)

    assert worker_main([str(manifest)]) == 0
    assert not manifest.exists()
    assert play.call_args_list == [
        call("/tools/ffplay", fails),
        call("/tools/ffplay", good),
    ]
    errors = capsys.readouterr().err
    assert str(bad) in errors and "invalid data" in errors
    assert str(fails) in errors and "status 1" in errors


def test_worker_warns_when_no_candidate_is_playable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = tmp_path / "bad.mp3"
    bad.touch()
    manifest = _manifest(tmp_path / "manifest.json", [bad])
    monkeypatch.setattr(
        "clp.worker._probe_audio", Mock(return_value=ProbeResult(error="invalid"))
    )

    assert worker_main([str(manifest)]) == 0
    assert "no playable audio files" in capsys.readouterr().err


def test_play_and_wait_runs_ffplay_synchronously(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_file = tmp_path / "sample.mp3"
    process = Mock()
    process.wait.return_value = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr("clp.worker.subprocess.Popen", popen)

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
