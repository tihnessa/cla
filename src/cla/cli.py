"""Command-line interface for background audio playback."""

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from cla.session import (
    CONTROL_COMMANDS,
    CONTROL_TIMEOUT_SECONDS,
    ControlResponse,
    QueueSnapshot,
    SessionDescriptor,
    is_seek_command,
    is_skip_command,
    parse_seek_command,
    parse_skip_command,
    playback_launch_lock,
    read_session,
    send_append,
    send_command,
)

FFMPEG_DOWNLOAD_URL = "https://ffmpeg.org/download.html"
PROBE_TIMEOUT_SECONDS = 10
STARTUP_GRACE_SECONDS = 5.0
STARTUP_POLL_INTERVAL_SECONDS = 0.01
WORKER_STOP_TIMEOUT_SECONDS = 2.0
AUDIO_EXTENSIONS = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"})
PLAYLIST_EXTENSION = ".m3u"
ARGUMENT_CONTROL_COMMANDS = frozenset({"ff", "rw", "skip"})


@dataclass(frozen=True)
class ProbeResult:
    """The useful result of probing one audio candidate."""

    track: Optional[int] = None
    disc: Optional[int] = None
    duration: Optional[float] = None
    title: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class FolderSources:
    """Playable source types discovered directly inside a folder."""

    audio_files: list[Path]
    playlists: list[Path]


@dataclass(frozen=True)
class ResolvedSource:
    """Candidate files and ordering semantics resolved from one CLI path."""

    files: list[Path]
    input_order_authoritative: bool = False
    kind: str = "playback"
    single_audio: bool = False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cla",
        description="Play local audio in the background or control active playback.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "queue: add <path>\n"
            "controls: pause, play, next, back/prev, replay, restart, kill, "
            "status, list\n"
            "arguments: skip[index|+/-offset] or skip [index|+/-offset]; "
            "ff[seconds] or ff [seconds]; rw[seconds] or rw [seconds]"
        ),
    )
    parser.add_argument(
        "target",
        metavar="path|command",
        help="audio file, M3U playlist, folder path, or playback control command",
    )
    parser.add_argument("path", nargs="?", help="source path for the add command")
    return parser


def _error(message: str) -> int:
    print(f"cla: error: {message}", file=sys.stderr)
    return 1


def _warning(subject: object, message: str) -> None:
    print(f"cla: warning: {subject!s}: {message}", file=sys.stderr)


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
    sources, error = _directory_sources(path)
    if error is not None:
        return [], error
    if not sources.audio_files:
        return [], f"{path!s} contains no supported audio files"
    return sources.audio_files, None


def _directory_sources(path: Path) -> tuple[FolderSources, Optional[str]]:
    try:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            return FolderSources([], []), f"{path!s} is not a readable directory"
        children = list(resolved.iterdir())
    except OSError:
        return FolderSources([], []), f"{path!s} is not a readable directory"

    audio_files = []
    playlists = []
    for child in children:
        suffix = child.suffix.casefold()
        if suffix not in AUDIO_EXTENSIONS and suffix != PLAYLIST_EXTENSION:
            continue
        try:
            if not child.is_file():
                continue
            candidate = child.resolve()
        except OSError:
            candidate = child.absolute()
        if suffix == PLAYLIST_EXTENSION:
            playlists.append(candidate)
        else:
            audio_files.append(candidate)

    if not audio_files and not playlists:
        return (
            FolderSources([], []),
            f"{path!s} contains no supported audio files or M3U playlists",
        )
    return FolderSources(
        sorted(audio_files, key=_natural_key),
        sorted(playlists, key=_natural_key),
    ), None


def _is_url_entry(value: str) -> bool:
    try:
        scheme = urlsplit(value).scheme
    except ValueError:
        return re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) is not None
    if not scheme:
        return False
    return not (len(scheme) == 1 and len(value) > 2 and value[1] == ":")


def _playlist_candidates(path: Path) -> tuple[list[Path], Optional[str]]:
    """Read supported local audio paths from an M3U in declared order."""
    try:
        playlist = path.expanduser().resolve()
        with playlist.open(encoding="utf-8-sig") as input_file:
            lines = input_file.readlines()
    except (OSError, UnicodeError) as error:
        return [], f"could not read playlist {path!s}: {error}"

    candidates = []
    for raw_line in lines:
        entry = raw_line.strip()
        if not entry or entry.startswith("#"):
            continue
        if _is_url_entry(entry):
            _warning(entry, "URL entries are not supported")
            continue
        candidate = Path(entry).expanduser()
        if candidate.suffix.casefold() not in AUDIO_EXTENSIONS:
            _warning(entry, "unsupported audio type")
            continue
        if not candidate.is_absolute():
            candidate = playlist.parent / candidate
        try:
            candidate = candidate.resolve()
        except OSError:
            candidate = candidate.absolute()
        candidates.append(candidate)

    if not candidates:
        return [], f"{path!s} contains no supported local audio entries"
    return candidates, None


def _numbered_choice(
    title: str, labels: Sequence[str]
) -> tuple[Optional[int], Optional[str]]:
    print(title)
    for number, label in enumerate(labels, start=1):
        print(f"{number}. {label}")
    try:
        answer = input("Selection (q to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None, "selection cancelled because interactive input is unavailable"
    if answer.casefold() == "q":
        return None, "selection cancelled"
    try:
        selected = int(answer)
    except ValueError:
        return None, "invalid selection"
    if not 1 <= selected <= len(labels):
        return None, "invalid selection"
    return selected - 1, None


def _select_folder_source(
    sources: FolderSources,
) -> tuple[list[Path], bool, Optional[str]]:
    use_playlist = not sources.audio_files
    if sources.audio_files and sources.playlists:
        selected, error = _numbered_choice(
            "Select a playback source:", ["Loose audio files", "M3U playlist"]
        )
        if error is not None:
            return [], False, error
        use_playlist = selected == 1

    if not use_playlist:
        return sources.audio_files, False, None

    playlist = sources.playlists[0]
    if len(sources.playlists) > 1:
        selected, error = _numbered_choice(
            "Select an M3U playlist:", [path.name for path in sources.playlists]
        )
        if error is not None:
            return [], False, error
        assert selected is not None
        playlist = sources.playlists[selected]
    candidates, error = _playlist_candidates(playlist)
    return candidates, True, error


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


def _metadata_text(tags: object, name: str) -> Optional[str]:
    if not isinstance(tags, Mapping):
        return None
    value = next(
        (value for key, value in tags.items() if str(key).casefold() == name),
        None,
    )
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


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
                    "stream=codec_type,duration:stream_tags=track,disc,title:"
                    "format=duration:format_tags=track,disc,title"
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
    title = _metadata_text(format_tags, "title") or _metadata_text(stream_tags, "title")
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
    return ProbeResult(track=track, disc=disc, duration=duration, title=title)


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


def _write_manifest(
    files: Sequence[Path],
    ffprobe: str,
    ffplay: str,
    *,
    input_order_authoritative: bool = False,
) -> Path:
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
                    "input_order_authoritative": input_order_authoritative,
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
        with tempfile.NamedTemporaryFile(
            prefix="cla-startup-", suffix=".json", delete=False
        ) as startup_file:
            startup_path = Path(startup_file.name)
        startup_path.unlink(missing_ok=True)
    except OSError as error:
        manifest.unlink(missing_ok=True)
        return f"could not prepare playback startup handshake: {error}"
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "cla.worker", str(manifest), str(startup_path)],
            **_background_options(),
        )
    except OSError as error:
        startup_path.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        return f"could not start folder playback: {error}"

    try:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            files = data.get("files") if isinstance(data, Mapping) else None
            probe_count = len(files) if isinstance(files, list) and files else 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            probe_count = 1
        deadline = (
            time.monotonic()
            + probe_count * PROBE_TIMEOUT_SECONDS
            + STARTUP_GRACE_SECONDS
        )
        while True:
            status = _read_startup_status(startup_path, process.pid)
            if status is not None:
                ok, error = status
                if ok:
                    returncode = process.poll()
                    if returncode is not None:
                        manifest.unlink(missing_ok=True)
                        return (
                            "playback worker exited with status "
                            f"{returncode} during startup"
                        )
                    if not _session_is_ready(process.pid):
                        _stop_worker(process)
                        manifest.unlink(missing_ok=True)
                        return "playback worker did not publish a reachable session"
                    returncode = process.poll()
                    if returncode is not None:
                        manifest.unlink(missing_ok=True)
                        return (
                            "playback worker exited with status "
                            f"{returncode} during startup"
                        )
                    return None
                _stop_worker(process)
                manifest.unlink(missing_ok=True)
                return error or "playback worker failed to start"

            returncode = process.poll()
            if returncode is not None:
                status = _read_startup_status(startup_path, process.pid)
                if status is not None:
                    ok, error = status
                    if ok:
                        manifest.unlink(missing_ok=True)
                        return (
                            "playback worker exited with status "
                            f"{returncode} during startup"
                        )
                    manifest.unlink(missing_ok=True)
                    return error or "playback worker failed to start"
                manifest.unlink(missing_ok=True)
                return f"playback worker exited with status {returncode} during startup"

            if time.monotonic() >= deadline:
                _stop_worker(process)
                manifest.unlink(missing_ok=True)
                return "playback worker timed out during startup"
            time.sleep(STARTUP_POLL_INTERVAL_SECONDS)
    finally:
        startup_path.unlink(missing_ok=True)


def _session_is_ready(expected_pid: int) -> bool:
    """Confirm the startup descriptor belongs to and reaches the new worker."""
    descriptor = read_session()
    if descriptor is None or descriptor.pid != expected_pid:
        return False
    response = send_command("_ping")
    current = read_session()
    return response.ok and current == descriptor


def _read_startup_status(
    path: Path, expected_pid: int
) -> Optional[tuple[bool, Optional[str]]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(data, Mapping)
        or data.get("pid") != expected_pid
        or not isinstance(data.get("ok"), bool)
    ):
        return None
    error = data.get("error")
    if error is not None and not isinstance(error, str):
        return None
    return data["ok"], error


def _stop_worker(process: object) -> None:
    """Stop a failed startup without leaving FFprobe or FFplay descendants."""
    try:
        if process.poll() is not None:  # type: ignore[attr-defined]
            return
        if os.name == "nt":
            _stop_windows_worker_tree(process)
        else:
            _stop_posix_worker_tree(process)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _stop_posix_worker_tree(process: object) -> None:
    """Signal the process group created for the detached worker."""
    pid = process.pid  # type: ignore[attr-defined]
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        process.terminate()  # type: ignore[attr-defined]

    try:
        process.wait(timeout=WORKER_STOP_TIMEOUT_SECONDS)  # type: ignore[attr-defined]
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            process.kill()  # type: ignore[attr-defined]
        process.wait(timeout=WORKER_STOP_TIMEOUT_SECONDS)  # type: ignore[attr-defined]
        return

    # The worker can exit before a descendant that ignored SIGTERM. Its process
    # group still has the worker PID as its ID, so ensure no member survives.
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def _stop_windows_worker_tree(process: object) -> None:
    """Use Windows' tree-aware termination for the worker process group."""
    result = subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],  # type: ignore[attr-defined]
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=WORKER_STOP_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 and process.poll() is None:  # type: ignore[attr-defined]
        process.kill()  # type: ignore[attr-defined]
    process.wait(timeout=WORKER_STOP_TIMEOUT_SECONDS)  # type: ignore[attr-defined]


def _wait_for_session_shutdown(
    descriptor: SessionDescriptor, timeout_message: str
) -> Optional[str]:
    """Wait for one identified worker to remove its session descriptor."""
    deadline = time.monotonic() + CONTROL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = read_session()
        if current is None:
            return None
        if current.token != descriptor.token:
            return "another playback session became active"
        time.sleep(STARTUP_POLL_INTERVAL_SECONDS)
    return timeout_message


def _stop_existing_session() -> Optional[str]:
    descriptor = read_session()
    if descriptor is None:
        return None
    response = send_command("_shutdown")
    if not response.ok:
        current = read_session()
        if current is not None and current.token == descriptor.token:
            return response.message or "could not stop existing playback session"
        return None

    return _wait_for_session_shutdown(
        descriptor, "existing playback session did not shut down"
    )


def _send_control(command: str) -> ControlResponse:
    """Send a public control, waiting for kill to finish session cleanup."""
    descriptor = read_session() if command == "kill" else None
    response = send_command(command)
    if command != "kill" or not response.ok or descriptor is None:
        return response

    error = _wait_for_session_shutdown(descriptor, "playback session did not shut down")
    return response if error is None else ControlResponse(False, error)


def _format_time(seconds: float) -> str:
    minutes, remaining_seconds = divmod(int(max(0.0, seconds)), 60)
    return f"{minutes:02d}:{remaining_seconds:02d}"


PAGER_PROMPT = "--More-- (Enter: line, Space: page, q: quit)"


def _terminal_height() -> int:
    """Return the usable terminal height, falling back when lookup fails."""
    try:
        return max(1, shutil.get_terminal_size(fallback=(80, 20)).lines)
    except OSError:
        return 20


def _read_pager_key() -> Optional[str]:
    """Read one key without requiring Enter, or return None when unavailable."""
    try:
        if not sys.stdin.isatty():
            return None
    except (AttributeError, OSError):
        return None

    if os.name == "nt":
        try:
            import msvcrt

            key = msvcrt.getwch()
        except (EOFError, KeyboardInterrupt, OSError):
            return None
        return key or None

    try:
        import termios
        import tty

        descriptor = sys.stdin.fileno()
        settings = termios.tcgetattr(descriptor)
        try:
            tty.setraw(descriptor)
            key = sys.stdin.read(1)
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, settings)
    except (AttributeError, EOFError, KeyboardInterrupt, OSError, ValueError):
        return None
    return key if key and key != "\x03" else None


def _queue_lines(queue: QueueSnapshot) -> list[str]:
    return [
        f"{index}. {'>' if index == queue.current_index else ' '} {label}"
        for index, label in enumerate(queue.labels, start=1)
    ]


def _print_queue(queue: QueueSnapshot) -> None:
    """Print a queue directly or page it when it exceeds terminal height."""
    lines = _queue_lines(queue)
    height = _terminal_height()
    if len(lines) <= height:
        for line in lines:
            print(line)
        return

    page_size = max(1, height - 1)
    position = min(page_size, len(lines))
    for line in lines[:position]:
        print(line)

    while position < len(lines):
        print(PAGER_PROMPT, end="", flush=True)
        key = _read_pager_key()
        print(f"\r{' ' * len(PAGER_PROMPT)}\r", end="", flush=True)
        if key is None or key.casefold() == "q":
            return
        if key in ("\r", "\n"):
            count = 1
        elif key == " ":
            count = page_size
        else:
            continue
        next_position = min(len(lines), position + count)
        for line in lines[position:next_position]:
            print(line)
        position = next_position


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


def _resolve_source(argument: str) -> tuple[Optional[ResolvedSource], Optional[str]]:
    """Resolve a CLI source identically for replacement and append operations."""
    target = Path(argument)
    try:
        resolved = target.expanduser().resolve()
        is_file = resolved.is_file()
        is_directory = resolved.is_dir()
    except OSError:
        return None, f"{target!s} is not a readable file or directory"

    if is_file:
        if resolved.suffix.casefold() == PLAYLIST_EXTENSION:
            candidates, error = _playlist_candidates(resolved)
            if error is not None:
                return None, error
            return ResolvedSource(candidates, True, "playlist"), None
        audio_file = _readable_file(resolved)
        if audio_file is None:
            return None, f"{target!s} is not a readable file"
        return ResolvedSource([audio_file], kind="playback", single_audio=True), None

    if is_directory:
        sources, error = _directory_sources(resolved)
        if error is not None:
            return None, error
        candidates, authoritative, error = _select_folder_source(sources)
        if error is not None:
            return None, error
        return ResolvedSource(candidates, authoritative, "folder"), None

    return None, f"{target!s} is not a readable file or directory"


def _source_manifest(source: ResolvedSource, ffprobe: str, ffplay: str) -> Path:
    if source.input_order_authoritative:
        return _write_manifest(
            source.files,
            ffprobe,
            ffplay,
            input_order_authoritative=True,
        )
    return _write_manifest(source.files, ffprobe, ffplay)


def _validate_single_audio(source: ResolvedSource, ffprobe: str) -> Optional[str]:
    if not source.single_audio:
        return None
    probe = _probe_audio(ffprobe, source.files[0])
    if probe.error is not None:
        return probe.error
    if probe.duration is None:
        return "could not determine audio duration"
    return None


def _coordination_error(source: ResolvedSource, error: Exception) -> str:
    return f"could not coordinate {source.kind} launch: {error}"


def _start_source_locked(
    source: ResolvedSource, ffprobe: str, ffplay: str
) -> Optional[str]:
    manifest = _source_manifest(source, ffprobe, ffplay)
    error = _start_worker(manifest)
    if error is not None:
        manifest.unlink(missing_ok=True)
    return error


def _add_source(argument: str) -> int:
    source, error = _resolve_source(argument)
    if error is not None:
        return _error(error)
    assert source is not None
    ffprobe, ffplay, error = _tools()
    if error is not None:
        return _error(error)
    assert ffprobe is not None and ffplay is not None

    error = _validate_single_audio(source, ffprobe)
    if error is not None:
        return _error(error)

    try:
        with playback_launch_lock():
            if read_session() is None:
                error = _start_source_locked(source, ffprobe, ffplay)
                return 0 if error is None else _error(error)

            manifest = _source_manifest(source, ffprobe, ffplay)
            try:
                response = send_append(manifest, candidate_count=len(source.files))
            finally:
                manifest.unlink(missing_ok=True)

            if response.unavailable:
                error = _start_source_locked(source, ffprobe, ffplay)
                return 0 if error is None else _error(error)
    except (OSError, TypeError) as caught:
        return _error(f"could not coordinate playlist addition: {caught}")

    for warning in response.warnings:
        print(f"cla: warning: {warning}", file=sys.stderr)
    if not response.ok:
        return _error(response.message or "could not append to playback queue")
    return 0


def _normalize_control_argv(argv: Sequence[str]) -> Sequence[str]:
    """Join spaced control values before argparse interprets option-like text."""
    if len(argv) == 2 and argv[0] in ARGUMENT_CONTROL_COMMANDS:
        return (f"{argv[0]}{argv[1]}",)
    return argv


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Start, extend, or control background playback."""
    parser = _parser()
    raw_argv = sys.argv[1:] if argv is None else argv
    arguments = parser.parse_args(_normalize_control_argv(raw_argv))
    if arguments.target == "add":
        if arguments.path is None:
            parser.error("add requires a path")
        return _add_source(arguments.path)
    if arguments.path is not None:
        parser.error("only add, ff, rw, and skip accept a second argument")

    argument = arguments.target
    unqualified_seek = Path(argument).name == argument and is_seek_command(argument)
    unqualified_skip = Path(argument).name == argument and is_skip_command(argument)
    if (
        argument in CONTROL_COMMANDS
        or (unqualified_seek and parse_seek_command(argument) is not None)
        or (unqualified_skip and parse_skip_command(argument) is not None)
    ):
        response = _send_control(argument)
        if argument == "status":
            if response.unavailable:
                print("Nothing in queue")
                return 0
            if not response.ok:
                return _error(response.message or "playback status failed")
            if response.status is None:
                return _error("invalid control response")
            status = response.status
            print(
                f"{status.title} — {_format_time(status.elapsed)} / "
                f"{_format_time(status.duration)}"
            )
            return 0
        if argument == "list":
            if response.unavailable:
                print("Nothing in queue")
                return 0
            if not response.ok:
                return _error(response.message or "playlist listing failed")
            if response.queue is None:
                return _error("invalid control response")
            _print_queue(response.queue)
            return 0
        if not response.ok:
            return _error(response.message or "playback control failed")
        if response.message is not None:
            print(f"cla: {response.message}")
        return 0
    if unqualified_seek or unqualified_skip:
        return 0

    source, error = _resolve_source(argument)
    if error is not None:
        return _error(error)
    assert source is not None
    ffprobe, ffplay, error = _tools()
    if error is not None:
        return _error(error)
    assert ffprobe is not None and ffplay is not None
    error = _validate_single_audio(source, ffprobe)
    if error is not None:
        return _error(error)

    try:
        with playback_launch_lock():
            error = _stop_existing_session()
            if error is not None:
                return _error(error)
            error = _start_source_locked(source, ffprobe, ffplay)
    except (OSError, TypeError) as caught:
        return _error(_coordination_error(source, caught))
    return 0 if error is None else _error(error)
