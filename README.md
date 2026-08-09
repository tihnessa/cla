# cla

A simple, cross-platform command-line audio player for Python 3.9 and newer.
It plays a file or folder in the background and returns control to the terminal
once playback has started and its control session is ready.

## Requirements

Both `ffplay` and `ffprobe` must be installed and available on `PATH`. They are
part of the FFmpeg project but may be packaged separately by some operating
systems. See the [official FFmpeg download page](https://ffmpeg.org/download.html)
for installation options.

`cla` has no third-party Python runtime dependencies. FFmpeg is a required host
application and is not bundled with the project.

## Usage

Install the command from a local checkout:

```bash
uv tool install .
```

Play a local audio file:

```bash
cla path/to/audio.mp3
```

Play the supported audio files directly inside a folder:

```bash
cla path/to/album
```

Control the active playback session from any terminal:

```bash
cla pause       # pause; repeated pauses are harmless
cla play        # resume; repeated plays are harmless
cla skip        # next track (alias: next)
cla back        # previous track (alias: prev)
cla ff          # seek forward 10 seconds
cla rw          # seek backward 10 seconds
cla replay      # restart the current track
cla restart     # restart the playlist from its first track
```

Starting another file or folder stops and replaces the current session. Concurrent
launch requests are serialized through replacement and startup, so only the most
recent ready worker remains active.
Navigation never wraps or changes the established playlist order. `skip` on
the final track and `back` on the first track leave playback unchanged and
print a clear message. Navigation, `replay`, and `restart` start the selected
track from `00:00`, including when playback was paused.

Seeking while paused keeps playback paused unless `ff` crosses the end of the
track, in which case the next track starts at `00:00`. `rw` clamps at `00:00`
instead of selecting the previous track. Fast-forwarding beyond the final
track stops playback. `restart` and `replay` are equivalent for a single-file
session. A control issued without an active session reports an error.

Bare control names are reserved: `pause`, `play`, `skip`, `next`, `back`,
`prev`, `ff`, `rw`, `replay`, and `restart` always control the active session.
To play a file or directory with one of those names, qualify it as a path, such
as `./next`, `../next`, `album/next`, or an absolute path.

Folder playback is non-recursive. It considers regular files with these
case-insensitive extensions: `.wav`, `.mp3`, `.flac`, `.ogg`, `.aac`, and
`.m4a`.

When every playable file has valid track metadata, files are ordered by disc
number, track number, and then natural filename order. Container metadata is
preferred over audio-stream metadata, and a missing disc number defaults to
disc 1. If any playable file lacks a valid track number, the entire folder uses
case-insensitive natural filename order, so `track2.mp3` precedes
`track10.mp3`.

The command produces no output when playback or a control succeeds, except for
first/last-track boundary messages. It validates files with `ffprobe`, launches
audio-only `ffplay` through a detached coordinator, and returns after playback
has started and its control session is ready. Folder tracks play sequentially.
Playback and later controls do not require the originating terminal to remain
open.

Supported formats depend on the installed FFmpeg build. Typical builds support
WAV, MP3, FLAC, OGG/Vorbis, and AAC/M4A. URLs and playlist files are not
supported.

Errors are written to standard error for missing or unreadable paths, folders
without matching files, missing FFmpeg tools, invalid or audio-less media,
validation timeouts, and failures to start playback. During folder playback, a
bad file produces an asynchronous warning and later tracks continue. Errors
that occur inside `ffplay` after startup may also appear in the terminal
asynchronously.

If a control takes too long, the command reports a timeout but preserves the
session because the playback worker may still be completing the operation. A
later control can be issued normally.

On Unix, control-session state is kept beneath a validated per-user runtime
directory (`$XDG_RUNTIME_DIR/cla` when available, otherwise `~/.cla/run`) with
permissions that prevent another local user from replacing its lock files.

## Development

Install the project and its development dependencies:

```bash
uv sync --dev
```

Run the command from the development environment:

```bash
uv run cla path/to/audio.mp3
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
