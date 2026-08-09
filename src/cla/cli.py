"""Command-line interface for background audio playback."""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cla.session import CONTROL_COMMANDS, read_session, send_command

FFMPEG_DOWNLOAD_URL = "https://ffmpeg.org/download.html"
PROBE_TIMEOUT_SECONDS = 10
AUDIO_EXTENSIONS = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"})


@dataclass(frozen=True)
class ProbeResult:
    """The useful result of probing one audio candidate."""

    track: Optional[int] = None
    disc: Optional[int] = None
    duration: Optional[float] = None
    error: Optional[str] = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cla",
        description="Play local audio in the background or control active playback.",
        epilog=(
            "controls: pause, play, skip/next, back/prev, ff, rew, replay, restart"
        ),
    )
    parser.add_argument(
        "target",
        metavar="path|command",
        help="audio file/folder path or playback control command",
    )
    return parser


def _error(message: str) -> int:
    print(f"cla: error: {message}", file=sys.stderr)
    return 1


def _readable_file(path: Path) -> Optional[Path]:
    try:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            return None
        with resolved.open("rb"):
            pass
    except OSError:
        return None
    return resolved


def _natural_key(path: Path) -> tuple[tuple[int, object], ...]:
    parts = re.split(r"(\d+)", path.name.casefold())
    key = tuple(
        (1, int(part)) if part.isdigit() else (0, part) for part in parts if part
    )
    return key + ((0, path.name),)


def _directory_candidates(path: Path) -> tuple[list[Path], Optional[str]]:
    try:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            return [], f"{path!s} is not a readable directory"
        children = list(resolved.iterdir())
    except OSError:
        return [], f"{path!s} is not a readable directory"

    candidates = []
    for child in children:
        if child.suffix.casefold() not in AUDIO_EXTENSIONS:
            continue
        try:
            if child.is_file():
                candidates.append(child.resolve())
        except OSError:
            candidates.append(child.absolute())

    if not candidates:
        return [], f"{path!s} contains no supported audio files"
    return sorted(candidates, key=_natural_key), None


def _find_tool(name: str) -> Optional[str]:
    return shutil.which(name)


def _metadata_number(tags: object, name: str) -> Optional[int]:
    if not isinstance(tags, Mapping):
        return None
    value = next(
        (value for key, value in tags.items() if str(key).casefold() == name),
        None,
    )
    match = re.match(r"\s*(\d+)", str(value)) if value is not None else None
    if match is None:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def _probe_audio(ffprobe: str, audio_file: Path) -> ProbeResult:
    try:
        result = subprocess.run(
            [
                ffprobe,
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
                str(audio_file),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(error="FFprobe timed out while inspecting the file")
    except OSError as error:
        return ProbeResult(error=f"could not start ffprobe: {error}")

    if result.returncode != 0:
        detail = result.stderr.strip() or "FFprobe could not read the file"
        return ProbeResult(error=detail)
    if not result.stdout.strip():
        return ProbeResult(error="the file does not contain an audio stream")

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError):
        return ProbeResult(error="FFprobe returned invalid metadata")
    if not isinstance(data, Mapping):
        return ProbeResult(error="FFprobe returned invalid metadata")

    streams = data.get("streams")
    if not isinstance(streams, list):
        streams = []
    audio_stream = next(
        (
            stream
            for stream in streams
            if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"
        ),
        None,
    )
    if audio_stream is None:
        return ProbeResult(error="the file does not contain an audio stream")

    format_data = data.get("format")
    format_tags = format_data.get("tags") if isinstance(format_data, Mapping) else None
    stream_tags = audio_stream.get("tags")
    track = _metadata_number(format_tags, "track") or _metadata_number(
        stream_tags, "track"
    )
    disc = _metadata_number(format_tags, "disc") or _metadata_number(
        stream_tags, "disc"
    )
    duration_value = (
        format_data.get("duration") if isinstance(format_data, Mapping) else None
    )
    if duration_value is None and isinstance(audio_stream, Mapping):
        duration_value = audio_stream.get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError):
        duration = None
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        duration = None
    return ProbeResult(track=track, disc=disc, duration=duration)


def _player_command(ffplay: str, audio_file: Path, start_at: float = 0.0) -> list[str]:
    command = [
        ffplay,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-nodisp",
        "-autoexit",
    ]
    if start_at > 0:
        command.extend(["-ss", f"{start_at:g}"])
    command.append(str(audio_file))
    return command


def _background_options() -> dict[str, object]:
    options: dict[str, object] = {
        "close_fds": True,
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
    }
    if os.name == "nt":
        options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        options["start_new_session"] = True
    return options


def _write_manifest(files: Sequence[Path], ffprobe: str, ffplay: str) -> Path:
    manifest_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="cla-",
            suffix=".json",
            delete=False,
        ) as manifest:
            manifest_path = Path(manifest.name)
            json.dump(
                {
                    "files": [str(path) for path in files],
                    "ffprobe": ffprobe,
                    "ffplay": ffplay,
                },
                manifest,
                ensure_ascii=False,
            )
    except (OSError, TypeError):
        if manifest_path is not None:
            manifest_path.unlink(missing_ok=True)
        raise
    return manifest_path


def _start_worker(manifest: Path) -> Optional[str]:
    if not sys.executable:
        return "could not determine the Python executable for folder playback"
    try:
        subprocess.Popen(
            [sys.executable, "-m", "cla.worker", str(manifest)],
            **_background_options(),
        )
    except OSError as error:
        return f"could not start folder playback: {error}"
    return None


def _tools() -> tuple[Optional[str], Optional[str], Optional[str]]:
    ffprobe = _find_tool("ffprobe")
    if ffprobe is None:
        return (
            None,
            None,
            (
                "ffprobe was not found on PATH; "
                f"install FFmpeg from {FFMPEG_DOWNLOAD_URL}"
            ),
        )
    ffplay = _find_tool("ffplay")
    if ffplay is None:
        return (
            None,
            None,
            (
                "ffplay was not found on PATH; "
                f"install FFmpeg from {FFMPEG_DOWNLOAD_URL}"
            ),
        )
    return ffprobe, ffplay, None


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Validate a file or folder and start background playback."""
    argument = _parser().parse_args(argv).target
    if argument in CONTROL_COMMANDS:
        response = send_command(argument)
        if not response.ok:
            return _error(response.message or "playback control failed")
        if response.message is not None:
            print(f"cla: {response.message}")
        return 0

    target = Path(argument)
    try:
        resolved = target.expanduser().resolve()
        is_file = resolved.is_file()
        is_directory = resolved.is_dir()
    except OSError:
        return _error(f"{target!s} is not a readable file or directory")

    if is_file:
        audio_file = _readable_file(resolved)
        if audio_file is None:
            return _error(f"{target!s} is not a readable file")
        ffprobe, ffplay, tool_error = _tools()
        if tool_error is not None:
            return _error(tool_error)
        assert ffprobe is not None and ffplay is not None
        probe = _probe_audio(ffprobe, audio_file)
        if probe.error is not None:
            return _error(probe.error)
        if probe.duration is None:
            return _error("could not determine audio duration")
        if read_session() is not None:
            send_command("_shutdown")
        try:
            manifest = _write_manifest([audio_file], ffprobe, ffplay)
        except (OSError, TypeError) as error:
            return _error(f"could not prepare playback: {error}")
        worker_error = _start_worker(manifest)
        if worker_error is not None:
            manifest.unlink(missing_ok=True)
            return _error(worker_error)
        return 0

    if is_directory:
        candidates, directory_error = _directory_candidates(resolved)
        if directory_error is not None:
            return _error(directory_error)
        ffprobe, ffplay, tool_error = _tools()
        if tool_error is not None:
            return _error(tool_error)
        assert ffprobe is not None and ffplay is not None
        if read_session() is not None:
            send_command("_shutdown")
        try:
            manifest = _write_manifest(candidates, ffprobe, ffplay)
        except (OSError, TypeError) as error:
            return _error(f"could not prepare folder playback: {error}")
        worker_error = _start_worker(manifest)
        if worker_error is not None:
            manifest.unlink(missing_ok=True)
            return _error(worker_error)
        return 0

    return _error(f"{target!s} is not a readable file or directory")
