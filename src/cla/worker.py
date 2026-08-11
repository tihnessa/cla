"""Background coordinator for stateful file and folder playback."""

import argparse
import json
import math
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cla.cli import ProbeResult, _natural_key, _player_command, _probe_audio
from cla.session import (
    COMMAND_ALIASES,
    MAX_MESSAGE_BYTES,
    ControlResponse,
    PlaybackStatus,
    QueueSnapshot,
    clear_session,
    encode_response,
    is_seek_command,
    is_skip_command,
    new_token,
    parse_seek_command,
    parse_skip_command,
    publish_session,
)

ACCEPT_TIMEOUT_SECONDS = 0.1
PROCESS_STOP_TIMEOUT_SECONDS = 2.0
PROCESS_KILL_TIMEOUT_SECONDS = 2.0
PLAYER_STARTUP_GRACE_SECONDS = 0.1
PLAYER_STARTUP_POLL_INTERVAL_SECONDS = 0.01


@dataclass(frozen=True)
class Track:
    """A playable file and its ordering and duration metadata."""

    path: Path
    track: Optional[int]
    disc: Optional[int]
    duration: float = math.inf
    title: Optional[str] = None


@dataclass(frozen=True)
class AppendRequest:
    """A request to validate and append candidates from a temporary manifest."""

    manifest: Path


def _warning(path: Optional[Path], message: str) -> None:
    subject = f"{path!s}: " if path is not None else ""
    print(f"cla: warning: {subject}{message}", file=sys.stderr, flush=True)


def _order_tracks(tracks: Sequence[Track]) -> list[Track]:
    if all(track.track is not None for track in tracks):
        return sorted(
            tracks,
            key=lambda item: (
                item.disc or 1,
                item.track,
                _natural_key(item.path),
            ),
        )
    return sorted(tracks, key=lambda item: _natural_key(item.path))


def _track_label(track: Track) -> str:
    """Return the public label shared by status and queue listings."""
    return track.title or track.path.name


class PlaybackController:
    """Own playlist state and translate commands into FFplay processes."""

    def __init__(
        self,
        ffplay: str,
        tracks: Sequence[Track],
        *,
        input_order_authoritative: bool = False,
        clock: Callable[[], float] = time.monotonic,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.ffplay = ffplay
        self.tracks = (
            list(tracks) if input_order_authoritative else _order_tracks(tracks)
        )
        self.clock = clock
        self.popen = popen
        self.index = 0
        self.offset = 0.0
        self.started_at = 0.0
        self.paused = False
        self.stopped = False
        self.exhausted = False
        self.closed = False
        self.process: Optional[Any] = None

    @property
    def current(self) -> Track:
        return self.tracks[self.index]

    def _position(self) -> float:
        if self.paused or self.process is None:
            return self.offset
        return min(self.current.duration, self.offset + self.clock() - self.started_at)

    def _launch(self) -> ControlResponse:
        try:
            self.process = self.popen(
                _player_command(self.ffplay, self.current.path, self.offset),
                close_fds=True,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
        except OSError as error:
            self.process = None
            self.stopped = True
            self.exhausted = False
            return ControlResponse(False, f"could not start ffplay: {error}")
        self.started_at = self.clock()
        self.stopped = False
        self.exhausted = False
        return ControlResponse(True)

    def _terminate(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=PROCESS_KILL_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                _warning(self.current.path, "ffplay did not exit after being killed")

    def _select(self, index: int) -> ControlResponse:
        self._terminate()
        self.index = index
        self.offset = 0.0
        self.paused = False
        return self._launch()

    def start(self) -> ControlResponse:
        """Start the first track in the established order."""
        return self._launch()

    def confirm_started(self) -> ControlResponse:
        """Confirm a newly launched player remains alive through startup."""
        deadline = time.monotonic() + PLAYER_STARTUP_GRACE_SECONDS
        while True:
            process = self.process
            returncode = process.poll() if process is not None else None
            if returncode is not None:
                self.process = None
                self.stopped = True
                self.exhausted = False
                return ControlResponse(
                    False, f"ffplay exited with status {returncode} during startup"
                )
            if time.monotonic() >= deadline:
                return ControlResponse(True)
            time.sleep(PLAYER_STARTUP_POLL_INTERVAL_SECONDS)

    def tick(self) -> None:
        """Advance after a player exits naturally."""
        if self.process is None:
            return
        returncode = self.process.poll()
        if returncode is None:
            return
        self.process = None
        if returncode != 0:
            _warning(self.current.path, f"ffplay exited with status {returncode}")
        if self.index == len(self.tracks) - 1:
            self.stopped = True
            self.exhausted = returncode == 0
            return
        self.index += 1
        self.offset = 0.0
        response = self._launch()
        if not response.ok:
            _warning(self.current.path, response.message or "could not start ffplay")
            self.stopped = True

    def shutdown(self) -> None:
        """Stop playback and close the session."""
        self._terminate()
        self.stopped = True
        self.exhausted = False
        self.closed = True

    def append(
        self,
        tracks: Sequence[Track],
        *,
        input_order_authoritative: bool = False,
    ) -> ControlResponse:
        """Atomically append a validated batch, reviving completed playback."""
        additions = list(tracks) if input_order_authoritative else _order_tracks(tracks)
        if not additions:
            return ControlResponse(False, "no playable audio files were found")

        original_length = len(self.tracks)
        original_index = self.index
        original_offset = self.offset
        original_paused = self.paused
        was_stopped = self.stopped
        was_exhausted = self.exhausted
        self.tracks.extend(additions)
        if not was_stopped:
            return ControlResponse(True)

        if was_exhausted:
            self.index = original_length
            self.offset = 0.0
            self.paused = False
        response = self._launch()
        if response.ok:
            response = self.confirm_started()
        if response.ok:
            return response

        del self.tracks[original_length:]
        self.index = original_index
        self.offset = original_offset
        self.paused = original_paused
        self.stopped = True
        self.exhausted = was_exhausted
        return response

    def handle(self, command: str) -> ControlResponse:
        """Apply one public playback command."""
        seek = parse_seek_command(command)
        skip = parse_skip_command(command)
        canonical = COMMAND_ALIASES.get(command)
        if canonical is None:
            if is_skip_command(command):
                if skip is None:
                    return ControlResponse(True)
                canonical = "skip"
            elif not is_seek_command(command):
                return ControlResponse(False, "unknown playback command")
            else:
                if seek is None:
                    return ControlResponse(True)
                canonical = seek[0]
        if canonical == "status":
            return ControlResponse(
                True,
                status=PlaybackStatus(
                    _track_label(self.current),
                    elapsed=self._position(),
                    duration=self.current.duration,
                ),
            )
        if canonical == "list":
            return ControlResponse(
                True,
                queue=QueueSnapshot(
                    tuple(_track_label(track) for track in self.tracks),
                    self.index + 1,
                ),
            )
        if canonical == "kill":
            self.shutdown()
            return ControlResponse(True)
        if canonical == "pause":
            if self.paused:
                return ControlResponse(True)
            self.offset = self._position()
            self._terminate()
            self.paused = True
            return ControlResponse(True)
        if canonical == "play":
            if not self.paused:
                return ControlResponse(True)
            self.paused = False
            return self._launch()
        if canonical == "skip":
            assert skip is not None
            kind, value = skip
            destination = value - 1 if kind == "absolute" else self.index + value
            if not 0 <= destination < len(self.tracks):
                message = (
                    "already on the last track"
                    if kind == "next"
                    else "value is out of range"
                )
                return ControlResponse(True, message)
            return self._select(destination)
        if canonical == "next":
            if self.index == len(self.tracks) - 1:
                return ControlResponse(True, "already on the last track")
            return self._select(self.index + 1)
        if canonical == "prev":
            if self.index == 0:
                return ControlResponse(True, "already on the first track")
            return self._select(self.index - 1)
        if canonical == "replay":
            return self._select(self.index)
        if canonical == "restart":
            return self._select(0)

        position = self._position()
        assert seek is not None
        seconds = seek[1]
        if canonical == "rw":
            new_offset = 0.0 if seconds >= position else position - seconds
            crosses_boundary = False
        else:
            crosses_boundary = seconds >= self.current.duration - position
            new_offset = (
                self.current.duration if crosses_boundary else position + seconds
            )
        was_paused = self.paused
        self._terminate()
        if canonical == "ff" and crosses_boundary:
            if self.index == len(self.tracks) - 1:
                self.stopped = True
                self.exhausted = True
                return ControlResponse(True)
            return self._select(self.index + 1)
        self.offset = new_offset
        if was_paused:
            self.paused = True
            return ControlResponse(True)
        return self._launch()


def _play_and_wait(ffplay: str, audio_file: Path) -> Optional[str]:
    """Compatibility helper retained for direct process tests."""
    try:
        process = subprocess.Popen(
            _player_command(ffplay, audio_file),
            close_fds=True,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        returncode = process.wait()
    except OSError as error:
        return f"could not start ffplay: {error}"
    if returncode != 0:
        return f"ffplay exited with status {returncode}"
    return None


def _read_manifest(path: Path) -> tuple[list[Path], str, str, bool]:
    try:
        with path.open(encoding="utf-8") as manifest:
            data = json.load(manifest)
    finally:
        path.unlink(missing_ok=True)

    if not isinstance(data, Mapping):
        raise ValueError("invalid playback manifest")
    files = data.get("files")
    ffprobe = data.get("ffprobe")
    ffplay = data.get("ffplay")
    input_order_authoritative = data.get("input_order_authoritative", False)
    if (
        not isinstance(files, list)
        or not all(isinstance(file, str) for file in files)
        or not isinstance(ffprobe, str)
        or not isinstance(ffplay, str)
        or not isinstance(input_order_authoritative, bool)
    ):
        raise ValueError("invalid playback manifest")
    return [Path(file) for file in files], ffprobe, ffplay, input_order_authoritative


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("startup_status", type=Path)
    return parser


def _publish_startup(path: Path, ok: bool, error: Optional[str] = None) -> None:
    """Atomically publish this worker's startup result for its parent CLI."""
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump({"pid": os.getpid(), "ok": ok, "error": error}, output)
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _read_request(connection: socket.socket, token: str) -> Optional[object]:
    data = bytearray()
    while len(data) <= MAX_MESSAGE_BYTES:
        chunk = connection.recv(min(1024, MAX_MESSAGE_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in chunk:
            break
    if len(data) > MAX_MESSAGE_BYTES or b"\n" not in data:
        return None
    try:
        request = json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(request, Mapping) or not isinstance(request.get("token"), str):
        return None
    if not secrets.compare_digest(request["token"], token):
        return None
    command = request.get("command")
    if command == "_append":
        manifest = request.get("manifest")
        if not isinstance(manifest, str) or not manifest:
            return None
        return AppendRequest(Path(manifest))
    return command if isinstance(command, str) else None


def _probe_tracks(
    files: Sequence[Path], ffprobe: str
) -> tuple[list[Track], tuple[str, ...]]:
    """Probe a complete candidate batch without mutating playback state."""
    tracks = []
    warnings = []
    for audio_file in files:
        try:
            with audio_file.open("rb"):
                pass
        except OSError:
            warnings.append(f"{audio_file!s}: file is not readable")
            continue
        result: ProbeResult = _probe_audio(ffprobe, audio_file)
        if result.error is not None:
            warnings.append(f"{audio_file!s}: {result.error}")
            continue
        if result.duration is None:
            warnings.append(f"{audio_file!s}: could not determine audio duration")
            continue
        tracks.append(
            Track(
                audio_file,
                track=result.track,
                disc=result.disc,
                duration=result.duration,
                title=result.title,
            )
        )
    return tracks, tuple(warnings)


def _handle_append(
    controller: PlaybackController, request: AppendRequest
) -> ControlResponse:
    """Validate an append manifest and mutate the queue only after validation."""
    try:
        files, ffprobe, _ffplay, authoritative = _read_manifest(request.manifest)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return ControlResponse(False, f"could not read append manifest: {error}")
    tracks, warnings = _probe_tracks(files, ffprobe)
    if not tracks:
        return ControlResponse(
            False,
            "no playable audio files were found",
            warnings=warnings,
        )
    response = controller.append(tracks, input_order_authoritative=authoritative)
    return ControlResponse(
        response.ok,
        response.message,
        status=response.status,
        queue=response.queue,
        unavailable=response.unavailable,
        warnings=warnings,
    )


def _serve(
    controller: PlaybackController, startup_status: Optional[Path] = None
) -> int:
    token = new_token()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(ACCEPT_TIMEOUT_SECONDS)
        session_published = False
        try:
            start = controller.start()
            if not start.ok:
                error = start.message or "could not start playback"
                _warning(controller.current.path, error)
                if startup_status is not None:
                    _publish_startup(startup_status, False, error)
                return 1
            start = controller.confirm_started()
            if not start.ok:
                error = start.message or "ffplay exited during startup"
                _warning(controller.current.path, error)
                if startup_status is not None:
                    _publish_startup(startup_status, False, error)
                return 1
            try:
                publish_session(server.getsockname()[1], token)
                session_published = True
                if startup_status is not None:
                    _publish_startup(startup_status, True)
            except OSError as error:
                message = f"could not publish playback session: {error}"
                _warning(None, message)
                if startup_status is not None:
                    try:
                        _publish_startup(startup_status, False, message)
                    except OSError:
                        pass
                return 1
            while not controller.closed:
                controller.tick()
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(1.0)
                    try:
                        command = _read_request(connection, token)
                        if command == "_shutdown":
                            controller.shutdown()
                            response = ControlResponse(True)
                        elif command == "_ping":
                            response = ControlResponse(True)
                        elif isinstance(command, AppendRequest):
                            response = _handle_append(controller, command)
                        elif command is None:
                            response = ControlResponse(False, "invalid control request")
                        else:
                            response = controller.handle(command)
                        connection.sendall(encode_response(response))
                    except OSError:
                        continue
            return 0
        finally:
            controller.shutdown()
            if session_published:
                clear_session(token)


def worker_main(argv: Optional[Sequence[str]] = None) -> int:
    """Probe, order, and control a file or folder playback snapshot."""
    arguments = _parser().parse_args(argv)
    manifest = arguments.manifest
    startup_status = arguments.startup_status
    try:
        files, ffprobe, ffplay, input_order_authoritative = _read_manifest(manifest)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        message = f"could not read playback manifest: {error}"
        _warning(None, message)
        try:
            _publish_startup(startup_status, False, message)
        except OSError:
            pass
        return 1

    tracks, warnings = _probe_tracks(files, ffprobe)
    for warning in warnings:
        _warning(None, warning)

    if not tracks:
        message = "no playable audio files were found"
        _warning(None, message)
        try:
            _publish_startup(startup_status, False, message)
        except OSError:
            pass
        return 1
    try:
        return _serve(
            PlaybackController(
                ffplay,
                tracks,
                input_order_authoritative=input_order_authoritative,
            ),
            startup_status,
        )
    except OSError as error:
        message = f"could not start playback worker: {error}"
        _warning(None, message)
        try:
            _publish_startup(startup_status, False, message)
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(worker_main())
