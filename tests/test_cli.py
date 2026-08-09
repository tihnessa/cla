import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from cla.cli import (
    FFMPEG_DOWNLOAD_URL,
    ProbeResult,
    _probe_audio,
    _stop_existing_session,
    main,
)
from cla.session import ControlResponse, SessionDescriptor


def _install_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = {"ffplay": "/tools/ffplay", "ffprobe": "/tools/ffprobe"}
    monkeypatch.setattr("cla.cli.shutil.which", tools.get)


def _successful_probe() -> subprocess.CompletedProcess[str]:
    output = {
        "streams": [{"codec_type": "audio"}],
        "format": {"duration": "42.5"},
    }
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(output), stderr="")


def test_help_exits_successfully(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--help"])

    assert error.value.code == 0
    assert "path" in capsys.readouterr().out


def test_audio_file_is_required() -> None:
    with pytest.raises(SystemExit) as error:
        main([])

    assert error.value.code == 2


def test_rejects_missing_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "missing"

    assert main([str(path)]) == 1
    assert "not a readable file or directory" in capsys.readouterr().err


def test_rejects_path_when_status_cannot_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "sample.mp3"
    path.touch()
    monkeypatch.setattr(Path, "is_file", Mock(side_effect=PermissionError))

    assert main([str(path)]) == 1
    assert "not a readable file or directory" in capsys.readouterr().err


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
    monkeypatch.setattr("cla.cli.shutil.which", tools.get)

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
    monkeypatch.setattr("cla.cli.subprocess.run", Mock(return_value=result))

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
        "cla.cli.subprocess.run",
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
        "cla.cli.subprocess.run",
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
        "cla.cli.subprocess.run", Mock(side_effect=OSError("cannot execute"))
    )

    assert main([str(path)]) == 1
    assert "cannot execute" in capsys.readouterr().err


def test_reports_failure_to_start_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "sample.mp3"
    path.touch()
    _install_tools(monkeypatch)
    monkeypatch.setattr(
        "cla.cli.subprocess.run", Mock(return_value=_successful_probe())
    )
    monkeypatch.setattr(
        "cla.cli.subprocess.Popen", Mock(side_effect=OSError("cannot execute"))
    )

    assert main([str(path)]) == 1
    assert "cannot execute" in capsys.readouterr().err


def test_replacement_waits_until_previous_session_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = SessionDescriptor(43210, "previous", 1234)
    read_session = Mock(side_effect=[previous, previous, None])
    shutdown = Mock(return_value=ControlResponse(True))
    monkeypatch.setattr("cla.cli.read_session", read_session)
    monkeypatch.setattr("cla.cli.send_command", shutdown)
    monkeypatch.setattr("cla.cli.time.sleep", Mock())

    assert _stop_existing_session() is None
    shutdown.assert_called_once_with("_shutdown")
    assert read_session.call_count == 3


def test_probes_and_starts_playback_in_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "sp ace-音声.mp3"
    path.touch()
    _install_tools(monkeypatch)
    probe = Mock(return_value=_successful_probe())

    class Process:
        pid = 1234

        def poll(self):
            return None

    def popen(command, **options):
        Path(command[-1]).write_text(
            json.dumps({"pid": 1234, "ok": True, "error": None}),
            encoding="utf-8",
        )
        return Process()

    player = Mock(side_effect=popen)
    monkeypatch.setattr("cla.cli.subprocess.run", probe)
    monkeypatch.setattr("cla.cli.subprocess.Popen", player)

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
            (
                "stream=codec_type,duration:stream_tags=track,disc:"
                "format=duration:format_tags=track,disc"
            ),
            "-of",
            "json",
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

    worker_command = player.call_args.args[0]
    assert worker_command[:3] == [os.sys.executable, "-m", "cla.worker"]
    assert player.call_args.kwargs == expected_options


def test_probe_prefers_container_track_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "sample.mp3"
    output = {
        "streams": [{"codec_type": "audio", "tags": {"track": "8/12", "disc": "2/2"}}],
        "format": {"tags": {"TRACK": "3/12", "DISC": "1/2"}},
    }
    monkeypatch.setattr(
        "cla.cli.subprocess.run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(output), stderr=""
            )
        ),
    )

    assert _probe_audio("/tools/ffprobe", path) == ProbeResult(track=3, disc=1)


def test_probe_falls_back_to_stream_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "sample.flac"
    output = {
        "streams": [{"codec_type": "audio", "tags": {"track": "4", "disc": "2"}}],
        "format": {"tags": {"track": "not-a-number"}},
    }
    monkeypatch.setattr(
        "cla.cli.subprocess.run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(output), stderr=""
            )
        ),
    )

    assert _probe_audio("/tools/ffprobe", path) == ProbeResult(track=4, disc=2)


def test_probe_rejects_malformed_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "sample.mp3"
    monkeypatch.setattr(
        "cla.cli.subprocess.run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout="not json", stderr=""
            )
        ),
    )

    result = _probe_audio("/tools/ffprobe", path)

    assert result.error == "FFprobe returned invalid metadata"
