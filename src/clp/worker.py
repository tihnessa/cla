"""Background coordinator for sequential folder playback."""

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from clp.cli import ProbeResult, _natural_key, _player_command, _probe_audio


@dataclass(frozen=True)
class Track:
    """A playable file and its optional ordering metadata."""

    path: Path
    track: Optional[int]
    disc: Optional[int]


def _warning(path: Optional[Path], message: str) -> None:
    subject = f"{path!s}: " if path is not None else ""
    print(f"clp: warning: {subject}{message}", file=sys.stderr, flush=True)


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


def _play_and_wait(ffplay: str, audio_file: Path) -> Optional[str]:
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
        raise ValueError("invalid folder playback manifest")
    files = data.get("files")
    ffprobe = data.get("ffprobe")
    ffplay = data.get("ffplay")
    if (
        not isinstance(files, list)
        or not all(isinstance(file, str) for file in files)
        or not isinstance(ffprobe, str)
        or not isinstance(ffplay, str)
    ):
        raise ValueError("invalid folder playback manifest")
    return [Path(file) for file in files], ffprobe, ffplay


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("manifest", type=Path)
    return parser


def worker_main(argv: Optional[Sequence[str]] = None) -> int:
    """Probe, order, and sequentially play a folder snapshot."""
    manifest = _parser().parse_args(argv).manifest
    try:
        files, ffprobe, ffplay = _read_manifest(manifest)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _warning(None, f"could not read folder playback manifest: {error}")
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
        tracks.append(Track(audio_file, track=result.track, disc=result.disc))

    if not tracks:
        _warning(None, "no playable audio files were found")
        return 0

    for track in _order_tracks(tracks):
        playback_error = _play_and_wait(ffplay, track.path)
        if playback_error is not None:
            _warning(track.path, playback_error)
    return 0


if __name__ == "__main__":
    raise SystemExit(worker_main())
