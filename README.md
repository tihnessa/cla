# clp

A simple, cross-platform command-line audio player for Python 3.9 and newer.
It starts playback in the background and immediately returns control to the
terminal.

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

The command produces no output when playback starts successfully. It validates
the file with `ffprobe`, launches audio-only `ffplay` in the background, and
returns immediately. Playback does not need to continue after the originating
terminal closes.

Supported formats depend on the installed FFmpeg build. Typical builds support
WAV, MP3, FLAC, OGG/Vorbis, and AAC/M4A. URLs, playlists, and playback controls
are not supported.

Errors are written to standard error for missing or unreadable files, missing
FFmpeg tools, invalid or audio-less media, validation timeouts, and failures to
start playback. Errors that occur inside `ffplay` after startup may appear in
the terminal asynchronously.

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
