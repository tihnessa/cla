import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from clp.cli import FFMPEG_DOWNLOAD_URL, main


def _install_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = {"ffplay": "/tools/ffplay", "ffprobe": "/tools/ffprobe"}
    monkeypatch.setattr("clp.cli.shutil.which", tools.get)


def _successful_probe() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout="audio\n", stderr="")


def test_help_exits_successfully(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--help"])

    assert error.value.code == 0
    assert "audio_file" in capsys.readouterr().out


def test_audio_file_is_required() -> None:
    with pytest.raises(SystemExit) as error:
        main([])

    assert error.value.code == 2


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_rejects_paths_that_are_not_files(
    kind: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / kind
    if kind == "directory":
        path.mkdir()

    assert main([str(path)]) == 1
    assert "not a readable file" in capsys.readouterr().err


def test_rejects_unreadable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "unreadable.mp3"
    path.touch()
    monkeypatch.setattr(Path, "open", Mock(side_effect=PermissionError))

    assert main([str(path)]) == 1
    assert "not a readable file" in capsys.readouterr().err


@pytest.mark.parametrize("missing_tool", ["ffprobe", "ffplay"])
def test_reports_missing_ffmpeg_tool(
    missing_tool: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "sample.mp3"
    path.touch()
    tools = {"ffprobe": "/tools/ffprobe", "ffplay": "/tools/ffplay"}
    tools[missing_tool] = None
    monkeypatch.setattr("clp.cli.shutil.which", tools.get)

    assert main([str(path)]) == 1
    error = capsys.readouterr().err
    assert missing_tool in error
    assert FFMPEG_DOWNLOAD_URL in error


def test_rejects_media_that_ffprobe_cannot_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "broken.mp3"
    path.touch()
    _install_tools(monkeypatch)
    result = subprocess.CompletedProcess([], 1, stdout="", stderr="Invalid data")
    monkeypatch.setattr("clp.cli.subprocess.run", Mock(return_value=result))

    assert main([str(path)]) == 1
    assert "Invalid data" in capsys.readouterr().err


def test_rejects_media_without_an_audio_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "video.mp4"
    path.touch()
    _install_tools(monkeypatch)
    monkeypatch.setattr(
        "clp.cli.subprocess.run",
        Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")),
    )

    assert main([str(path)]) == 1
    assert "does not contain an audio stream" in capsys.readouterr().err


def test_reports_ffprobe_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "slow.mp3"
    path.touch()
    _install_tools(monkeypatch)
    monkeypatch.setattr(
        "clp.cli.subprocess.run",
        Mock(side_effect=subprocess.TimeoutExpired("ffprobe", 10)),
    )

    assert main([str(path)]) == 1
    assert "timed out" in capsys.readouterr().err


def test_reports_failure_to_start_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "sample.mp3"
    path.touch()
    _install_tools(monkeypatch)
    monkeypatch.setattr(
        "clp.cli.subprocess.run", Mock(side_effect=OSError("cannot execute"))
    )

    assert main([str(path)]) == 1
    assert "cannot execute" in capsys.readouterr().err


def test_reports_failure_to_start_ffplay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "sample.mp3"
    path.touch()
    _install_tools(monkeypatch)
    monkeypatch.setattr(
        "clp.cli.subprocess.run", Mock(return_value=_successful_probe())
    )
    monkeypatch.setattr(
        "clp.cli.subprocess.Popen", Mock(side_effect=OSError("cannot execute"))
    )

    assert main([str(path)]) == 1
    assert "cannot execute" in capsys.readouterr().err


def test_probes_and_starts_playback_in_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "sp ace-音声.mp3"
    path.touch()
    _install_tools(monkeypatch)
    probe = Mock(return_value=_successful_probe())
    player = Mock()
    monkeypatch.setattr("clp.cli.subprocess.run", probe)
    monkeypatch.setattr("clp.cli.subprocess.Popen", player)

    assert main([str(path)]) == 0
    assert capsys.readouterr() == ("", "")

    absolute_path = str(path.resolve())
    probe.assert_called_once_with(
        [
            "/tools/ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            absolute_path,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

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
            "/tools/ffplay",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-nodisp",
            "-autoexit",
            absolute_path,
        ],
        **expected_options,
    )
