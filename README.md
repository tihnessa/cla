# clp

A simple, cross-platform command-line audio player for Python 3.9 and newer.
It plays a file or folder in the background and immediately returns control to
the terminal.

## Requirements

Both `ffplay` and `ffprobe` must be installed and available on `PATH`. They are
part of the FFmpeg project but may be packaged separately by some operating
systems. See the [official FFmpeg download page](https://ffmpeg.org/download.html)
for installation options.

`clp` has no third-party Python runtime dependencies. FFmpeg is a required host
application and is not bundled with the project.

## Usage

Install the command from a local checkout:

```bash
uv tool install .
```

Play a local audio file:

```bash
clp path/to/audio.mp3
```

Play the supported audio files directly inside a folder:

```bash
clp path/to/album
```

Folder playback is non-recursive. It considers regular files with these
case-insensitive extensions: `.wav`, `.mp3`, `.flac`, `.ogg`, `.aac`, and
`.m4a`.

When every playable file has valid track metadata, files are ordered by disc
number, track number, and then natural filename order. Container metadata is
preferred over audio-stream metadata, and a missing disc number defaults to
disc 1. If any playable file lacks a valid track number, the entire folder uses
case-insensitive natural filename order, so `track2.mp3` precedes
`track10.mp3`.

The command produces no output when playback starts successfully. It validates
files with `ffprobe`, launches audio-only `ffplay` in the background, and
returns immediately. Folder tracks play sequentially. Playback does not need to
continue after the originating terminal closes.

Supported formats depend on the installed FFmpeg build. Typical builds support
WAV, MP3, FLAC, OGG/Vorbis, and AAC/M4A. URLs, playlists, and playback controls
are not supported.

Errors are written to standard error for missing or unreadable paths, folders
without matching files, missing FFmpeg tools, invalid or audio-less media,
validation timeouts, and failures to start playback. During folder playback, a
bad file produces an asynchronous warning and later tracks continue. Errors
that occur inside `ffplay` after startup may also appear in the terminal
asynchronously.

## Development

Install the project and its development dependencies:

```bash
uv sync --dev
```

Run the command from the development environment:

```bash
uv run clp path/to/audio.mp3
```

Run the checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Apply automatic lint and formatting fixes with:

```bash
uv run ruff check --fix .
uv run ruff format .
```
