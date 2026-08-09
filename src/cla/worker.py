"""Background coordinator for stateful file and folder playback."""

import argparse
import json
import math
import secrets
import socket
import subprocess
import sys
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
    clear_session,
    encode_response,
    new_token,
    publish_session,
)

ACCEPT_TIMEOUT_SECONDS = 0.1
PROCESS_STOP_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class Track:
    """A playable file and its ordering and duration metadata."""

    path: Path
    track: Optional[int]
    disc: Optional[int]
    duration: float = math.inf


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


class PlaybackController:
    """Own playlist state and translate commands into FFplay processes."""

    def __init__(
        self,
        ffplay: str,
        tracks: Sequence[Track],
        *,
        clock: Callable[[], float] = time.monotonic,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.ffplay = ffplay
        self.tracks = _order_tracks(tracks)
        self.clock = clock
        self.popen = popen
        self.index = 0
        self.offset = 0.0
        self.started_at = 0.0
        self.paused = False
        self.stopped = False
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
            return ControlResponse(False, f"could not start ffplay: {error}")
        self.started_at = self.clock()
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
            process.wait()

    def _select(self, index: int) -> ControlResponse:
        self._terminate()
        self.index = index
        self.offset = 0.0
        self.paused = False
        return self._launch()

    def start(self) -> ControlResponse:
        """Start the first track in the established order."""
        return self._launch()

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

    def handle(self, command: str) -> ControlResponse:
        """Apply one public playback command."""
        canonical = COMMAND_ALIASES.get(command)
        if canonical is None:
            return ControlResponse(False, "unknown playback command")
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
        new_offset = (
            max(0.0, position - 10.0) if canonical == "rew" else position + 10.0
        )
        was_paused = self.paused
        self._terminate()
        if canonical == "ff" and new_offset >= self.current.duration:
            if self.index == len(self.tracks) - 1:
                self.stopped = True
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


def _read_manifest(path: Path) -> tuple[list[Path], str, str]:
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
    if (
        not isinstance(files, list)
        or not all(isinstance(file, str) for file in files)
        or not isinstance(ffprobe, str)
        or not isinstance(ffplay, str)
    ):
        raise ValueError("invalid playback manifest")
    return [Path(file) for file in files], ffprobe, ffplay


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("manifest", type=Path)
    return parser


def _read_request(connection: socket.socket, token: str) -> Optional[str]:
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
    return command if isinstance(command, str) else None


def _serve(controller: PlaybackController) -> int:
    token = new_token()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(ACCEPT_TIMEOUT_SECONDS)
        publish_session(server.getsockname()[1], token)
        try:
            start = controller.start()
            if not start.ok:
                _warning(controller.current.path, start.message or "could not start")
                return 1
            while not controller.stopped:
                controller.tick()
                if controller.stopped:
                    break
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
            clear_session(token)


def worker_main(argv: Optional[Sequence[str]] = None) -> int:
    """Probe, order, and control a file or folder playback snapshot."""
    manifest = _parser().parse_args(argv).manifest
    try:
        files, ffprobe, ffplay = _read_manifest(manifest)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _warning(None, f"could not read playback manifest: {error}")
        return 1

    tracks = []
    for audio_file in files:
        try:
            with audio_file.open("rb"):
                pass
        except OSError:
            _warning(audio_file, "file is not readable")
            continue
        result: ProbeResult = _probe_audio(ffprobe, audio_file)
        if result.error is not None:
            _warning(audio_file, result.error)
            continue
        if result.duration is None:
            _warning(audio_file, "could not determine audio duration")
            continue
        tracks.append(
            Track(
                audio_file,
                track=result.track,
                disc=result.disc,
                duration=result.duration,
            )
        )

    if not tracks:
        _warning(None, "no playable audio files were found")
        return 0
    return _serve(PlaybackController(ffplay, tracks))


if __name__ == "__main__":
    raise SystemExit(worker_main())
