"""Local control protocol for the detached playback worker."""

import getpass
import hashlib
import json
import os
import secrets
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 4096
CONTROL_TIMEOUT_SECONDS = 1.0
COMMAND_ALIASES = {
    "skip": "next",
    "next": "next",
    "back": "prev",
    "prev": "prev",
    "pause": "pause",
    "play": "play",
    "ff": "ff",
    "rew": "rew",
    "replay": "replay",
    "restart": "restart",
}
CONTROL_COMMANDS = frozenset(COMMAND_ALIASES)


@dataclass(frozen=True)
class ControlResponse:
    """A response returned by the playback worker."""

    ok: bool
    message: Optional[str] = None


@dataclass(frozen=True)
class SessionDescriptor:
    """Connection details for the active per-user playback worker."""

    port: int
    token: str
    pid: int


def _session_path() -> Path:
    override = os.environ.get("CLA_SESSION_FILE")
    if override:
        return Path(override)
    identity = f"{getpass.getuser()}:{Path.home()}".encode()
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"cla-session-{suffix}.json"


def new_token() -> str:
    """Create an unguessable token for one worker session."""
    return secrets.token_urlsafe(32)


def read_session() -> Optional[SessionDescriptor]:
    """Read and validate the published session descriptor."""
    try:
        data = json.loads(_session_path().read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("version") != PROTOCOL_VERSION
            or not isinstance(data.get("port"), int)
            or not 0 < data["port"] < 65536
            or not isinstance(data.get("token"), str)
            or not data["token"]
            or not isinstance(data.get("pid"), int)
            or data["pid"] <= 0
        ):
            return None
        return SessionDescriptor(data["port"], data["token"], data["pid"])
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def publish_session(port: int, token: str) -> SessionDescriptor:
    """Atomically publish connection details for a worker."""
    descriptor = SessionDescriptor(port, token, os.getpid())
    destination = _session_path()
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{destination.name}-",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(
                {
                    "version": PROTOCOL_VERSION,
                    "port": port,
                    "token": token,
                    "pid": descriptor.pid,
                },
                output,
            )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, destination)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return descriptor


def clear_session(token: Optional[str] = None) -> None:
    """Remove a stale descriptor, optionally only when its token matches."""
    path = _session_path()
    if token is not None:
        descriptor = read_session()
        if descriptor is None or descriptor.token != token:
            return
    path.unlink(missing_ok=True)


def _receive_line(connection: socket.socket) -> bytes:
    data = bytearray()
    while len(data) <= MAX_MESSAGE_BYTES:
        chunk = connection.recv(min(1024, MAX_MESSAGE_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in chunk:
            break
    if len(data) > MAX_MESSAGE_BYTES or b"\n" not in data:
        raise ValueError("invalid control response")
    return bytes(data).split(b"\n", 1)[0]


def send_command(command: str) -> ControlResponse:
    """Send a command to the current worker, cleaning stale state on failure."""
    descriptor = read_session()
    if descriptor is None:
        clear_session()
        return ControlResponse(False, "no active playback session")
    request = (
        json.dumps(
            {"token": descriptor.token, "command": command}, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
    try:
        with socket.create_connection(
            ("127.0.0.1", descriptor.port), timeout=CONTROL_TIMEOUT_SECONDS
        ) as connection:
            connection.settimeout(CONTROL_TIMEOUT_SECONDS)
            connection.sendall(request)
            response_data = json.loads(_receive_line(connection).decode("utf-8"))
        if not isinstance(response_data, dict) or not isinstance(
            response_data.get("ok"), bool
        ):
            raise ValueError("invalid control response")
        message = response_data.get("message")
        if message is not None and not isinstance(message, str):
            raise ValueError("invalid control response")
        return ControlResponse(response_data["ok"], message)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        current = read_session()
        if current is not None and current.token == descriptor.token:
            clear_session(descriptor.token)
        return ControlResponse(False, "no active playback session")


def encode_response(response: ControlResponse) -> bytes:
    """Encode one worker response."""
    return (
        json.dumps(
            {"ok": response.ok, "message": response.message}, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
