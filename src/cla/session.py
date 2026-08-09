"""Local control protocol for the detached playback worker."""

import errno
import getpass
import hashlib
import json
import os
import secrets
import socket
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 4096
# A controller operation may spend two seconds waiting for FFplay to terminate,
# then another two seconds waiting after a forced kill.
CONTROL_TIMEOUT_SECONDS = 5.0
WINDOWS_LOCK_RETRY_SECONDS = 0.05
COMMAND_ALIASES = {
    "kill": "kill",
    "skip": "next",
    "next": "next",
    "back": "prev",
    "prev": "prev",
    "pause": "pause",
    "play": "play",
    "ff": "ff",
    "rw": "rw",
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
    runtime_directory = _default_runtime_directory()
    return runtime_directory / "session.json"


def _default_runtime_directory() -> Path:
    """Return state storage beneath a namespace other users cannot reserve."""
    if os.name == "nt":
        identity = f"{getpass.getuser()}:{Path.home()}".encode()
        suffix = hashlib.sha256(identity).hexdigest()[:16]
        runtime_directory = Path(tempfile.gettempdir()) / f"cla-{suffix}"
        _ensure_private_directory(runtime_directory)
        return runtime_directory

    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        root = Path(xdg_runtime)
        try:
            _validate_user_owned_root(root, private=True)
        except OSError:
            pass
        else:
            runtime_directory = root / "cla"
            _ensure_private_directory(runtime_directory)
            return runtime_directory

    home = Path.home()
    _validate_user_owned_root(home, private=False)
    application_directory = home / ".cla"
    _ensure_private_directory(application_directory)
    runtime_directory = application_directory / "run"
    _ensure_private_directory(runtime_directory)
    return runtime_directory


def _validate_user_owned_root(path: Path, *, private: bool) -> None:
    """Validate an existing root before creating predictable state beneath it."""
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"playback runtime root is not a directory: {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise PermissionError(
            f"playback runtime root is not owned by this user: {path}"
        )
    mode = stat.S_IMODE(metadata.st_mode)
    if private and mode != 0o700:
        raise PermissionError(f"playback runtime root does not have mode 0700: {path}")
    if not private and mode & 0o022:
        raise PermissionError(
            f"playback runtime root is writable by another user: {path}"
        )


def _ensure_private_directory(path: Path) -> None:
    """Create and validate a directory accessible only to the current user."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass

    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"playback runtime path is not a directory: {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise PermissionError(
            f"playback runtime directory is not owned by this user: {path}"
        )
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PermissionError(
            f"playback runtime directory does not have mode 0700: {path}"
        )


def _launch_lock_path() -> Path:
    session_path = _session_path()
    return session_path.with_name(f"{session_path.name}.launch.lock")


def _descriptor_lock_path() -> Path:
    session_path = _session_path()
    return session_path.with_name(f"{session_path.name}.descriptor.lock")


@contextmanager
def _open_lock_file(path: Path) -> Iterator[object]:
    """Safely create a private regular lock file without following symlinks."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow:
        flags |= no_follow
    else:
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                raise OSError(f"playback lock path is a symbolic link: {path}")
        except FileNotFoundError:
            pass
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"playback lock path is not a regular file: {path}")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError(
                f"playback lock file is not owned by this user: {path}"
            )
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError(f"playback lock file is not private: {path}")
        with os.fdopen(descriptor, "r+b") as lock_file:
            descriptor = -1
            yield lock_file
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _acquire_windows_lock(lock_file: object, msvcrt: object) -> None:
    """Wait until byte zero can be locked, without the CRT's retry limit."""
    while True:
        try:
            msvcrt.locking(  # type: ignore[attr-defined]
                lock_file.fileno(),
                msvcrt.LK_NBLCK,
                1,  # type: ignore[attr-defined]
            )
            return
        except OSError as error:
            winerror = getattr(error, "winerror", None)
            if winerror not in (None, 33) or (
                winerror is None
                and error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
            ):
                raise
            time.sleep(WINDOWS_LOCK_RETRY_SECONDS)


@contextmanager
def playback_launch_lock() -> Iterator[None]:
    """Serialize replacement and startup of the per-user playback worker."""
    path = _launch_lock_path()
    with _open_lock_file(path) as lock_file:
        if os.name == "nt":
            import msvcrt

            # Windows permits locking a range beyond end-of-file. Do not read
            # or initialize byte zero: another launcher may already own it.
            lock_file.seek(0)
            _acquire_windows_lock(lock_file, msvcrt)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _session_descriptor_lock() -> Iterator[None]:
    """Serialize descriptor replacement and token-checked removal."""
    path = _descriptor_lock_path()
    with _open_lock_file(path) as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            _acquire_windows_lock(lock_file, msvcrt)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
        with _session_descriptor_lock():
            os.replace(temporary, destination)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return descriptor


def clear_session(token: Optional[str] = None) -> None:
    """Remove matching stale state, or malformed state when no token is given."""
    path = _session_path()
    with _session_descriptor_lock():
        descriptor = read_session()
        if token is None:
            if descriptor is not None:
                return
        elif descriptor is None or descriptor.token != token:
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
    except socket.timeout:
        return ControlResponse(
            False, "playback control timed out; session may still be active"
        )
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
