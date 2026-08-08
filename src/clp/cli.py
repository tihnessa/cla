"""Command-line interface for background audio playback."""

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

FFMPEG_DOWNLOAD_URL = "https://ffmpeg.org/download.html"
PROBE_TIMEOUT_SECONDS = 10


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clp",
        description="Play a local audio file in the background.",
    )
    parser.add_argument("audio_file", type=Path, help="path to a local audio file")
    return parser


def _error(message: str) -> int:
    print(f"clp: error: {message}", file=sys.stderr)
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


def _find_tool(name: str) -> Optional[str]:
    return shutil.which(name)


def _probe_audio(ffprobe: str, audio_file: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_file),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "FFprobe timed out while inspecting the file"
    except OSError as error:
        return f"could not start ffprobe: {error}"

    if result.returncode != 0:
        detail = result.stderr.strip() or "FFprobe could not read the file"
        return detail
    if "audio" not in result.stdout.splitlines():
        return "the file does not contain an audio stream"
    return None


def _start_player(ffplay: str, audio_file: Path) -> Optional[str]:
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

    try:
        subprocess.Popen(
            [
                ffplay,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostats",
                "-nodisp",
                "-autoexit",
                str(audio_file),
            ],
            **options,
        )
    except OSError as error:
        return f"could not start ffplay: {error}"
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Validate an audio file and start background playback."""
    arguments = _parser().parse_args(argv)
    audio_file = _readable_file(arguments.audio_file)
    if audio_file is None:
        return _error(f"{arguments.audio_file!s} is not a readable file")

    ffprobe = _find_tool("ffprobe")
    if ffprobe is None:
        return _error(
            f"ffprobe was not found on PATH; install FFmpeg from {FFMPEG_DOWNLOAD_URL}"
        )
    ffplay = _find_tool("ffplay")
    if ffplay is None:
        return _error(
            f"ffplay was not found on PATH; install FFmpeg from {FFMPEG_DOWNLOAD_URL}"
        )

    probe_error = _probe_audio(ffprobe, audio_file)
    if probe_error is not None:
        return _error(probe_error)

    playback_error = _start_player(ffplay, audio_file)
    if playback_error is not None:
        return _error(playback_error)
    return 0
