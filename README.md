# cla

A simple, cross-platform command-line audio player for Python 3.9 and newer.
It plays a file, M3U playlist, or folder in the background and returns control
to the terminal once playback has started and its control session is ready.

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

Play a local M3U playlist in its declared order:

```bash
cla path/to/mix.m3u
```

Append a file, folder, or M3U playlist to the active queue:

```bash
cla add path/to/audio.mp3
cla add path/to/album
cla add path/to/mix.m3u
```

Control the active playback session from any terminal:

```bash
cla pause       # pause; repeated pauses are harmless
cla play        # resume; repeated plays are harmless
cla skip        # next track (alias: next)
cla skip12      # jump to playlist entry 12
cla skip 12     # equivalent spaced form
cla skip+3      # jump forward three entries
cla skip +3     # equivalent spaced form
cla skip-2      # jump backward two entries
cla skip -2     # equivalent spaced form
cla back        # previous track (alias: prev)
cla ff          # seek forward 10 seconds
cla ff20        # seek forward 20 seconds
cla ff 20       # equivalent spaced form
cla rw          # seek backward 10 seconds
cla rw30        # seek backward 30 seconds
cla rw 30       # equivalent spaced form
cla replay      # restart the current track
cla restart     # restart the playlist from its first track
cla kill        # stop playback and discard the playlist
cla status      # show the current track and playback position
cla list        # show the complete queue in playback order
```

`cla status` prints the embedded track title and its elapsed and total time:

```text
Track title — 01:23 / 03:45
```

Container title metadata is preferred over audio-stream title metadata. If no
non-empty title is available, only the file name is shown; parent directories
and absolute paths are never included. Times use zero-padded minutes and
seconds, with minutes continuing past 59 for longer tracks. While playback is
paused, the displayed elapsed time remains frozen. If no playback queue is
active, `cla status` prints `Nothing in queue` and succeeds.

`cla list` prints every queued track with a stable one-based index. The current
track is marked with `>`, separately from its index:

```text
1.   Opening track
2. > Current track
3.   closing-track.mp3
```

The labels follow the same title and filename rules as `cla status`. A queue
that fits within the terminal is printed directly. Longer queues reserve the
last visible row for a prompt: press Enter to reveal one more entry, Space to
advance one page, or `q` to stop. Terminal-height detection falls back to 20
rows. End-of-input or unavailable interactive input stops paging cleanly. Like
`status`, `list` prints `Nothing in queue` and succeeds when no queue is active.

Starting another file, playlist, or folder stops and replaces the current session.
Concurrent launch requests are serialized through replacement and startup, so only
the most recent ready worker remains active.
`cla add` instead appends the resolved tracks after everything already queued and
does not interrupt the current track. Additions use the same discovery, selection,
ordering, validation, and warning rules as normal playback. M3U order remains
authoritative, while a folder's newly added tracks are ordered as one batch without
reordering tracks that were already queued. A clean successful addition produces no
output.

The playback session remains available after its final track ends. Adding playable
material to that completed queue starts playback automatically from the first new
track. If no session exists, `cla add` starts a new queue just like normal playback.
Candidates are fully probed before the live queue is changed; cancellation or a
batch containing no playable files leaves the existing queue untouched. Concurrent
additions and replacement launches are serialized. `kill` still stops playback and
discards the queue.
Navigation never wraps or changes the established playlist order. Append a
positive one-based index directly to `skip`, or pass it as a separate argument,
to select that queue entry. Use a joined or separate `+` or `-` value to move
relative to the current track. For example, `skip12` and `skip 12`, `skip+3`
and `skip +3`, and `skip-2` and `skip -2` are equivalent. The `next` alias
remains a single-step command and does not accept numbered forms.

Bare `skip` on the final track and `back` on the first track leave playback
unchanged and print a clear message. A numbered skip that would land before
entry 1 or after the final entry also leaves playback unchanged and prints
`value is out of range`; jumps never wrap or clamp. Navigation, `replay`, and
`restart` start the selected track from `00:00`, including when playback was
paused. Selecting the current track by its absolute index restarts it.

Seeking while paused keeps playback paused unless `ff` crosses the end of the
track, in which case the next track starts at `00:00`. `rw` clamps at `00:00`
instead of selecting the previous track. Fast-forwarding beyond the final
track stops playback. Append a positive whole number of seconds directly to
`ff` or `rw`, or pass it as a separate argument, to choose the seek distance;
without a number, the distance is 10 seconds. Thus `ff20` and `ff 20` are
equivalent, as are `rw30` and `rw 30`. Invalid or zero values, such as `ff0`,
`ff 0`, `rw-5`, `rw -5`, or `ffabc`, are ignored. Invalid compact or spaced
skip values are likewise silent no-ops. These include zero, signed zero,
missing numbers, non-decimal values, repeated signs, and values larger than the
supported command size. Seek and skip numbers use ASCII decimal digits.
`restart` and `replay` are equivalent for a single-file session. A control
issued without an active session reports an error, except for `status` and
`list`, which report an empty queue as described above.

The bare command name `add` is also reserved. To play a file or directory named
`add`, qualify it as a path, such as `./add`.

Bare control names are reserved: `pause`, `play`, `skip`, `next`, `back`,
`prev`, `ff`, `rw`, `replay`, `restart`, `kill`, and `status` always address
the active playback session. `list` is reserved in the same way. Unqualified
names beginning with `ff` or `rw` are also reserved for custom or invalid seek
controls. Unqualified names beginning with `skip` are reserved in the same way
for valid or invalid skip controls. Exact `ff`, `rw`, and `skip` commands with a
separate argument are reserved in the same way. To play a file or directory
with one of those names, qualify it as a path, such as `./next`, `./list`,
`./ff20`, `./skip12`, `../next`, `album/next`, or an absolute path.

Folder playback is non-recursive. It considers regular files with these
case-insensitive extensions: `.wav`, `.mp3`, `.flac`, `.ogg`, `.aac`, and
`.m4a`, as well as `.m3u` playlists. A folder containing one playlist and no
loose audio tracks plays that playlist automatically. Multiple playlists, or a
mixture of playlists and loose tracks, produce deterministic numbered choices.
Enter `q` to cancel. Invalid, cancelled, or unavailable interactive input is
reported without replacing an active playback session. Audio and playlists in
nested folders are not discovered, although a chosen playlist may explicitly
refer to audio in another folder.

When every playable file has valid track metadata, files are ordered by disc
number, track number, and then natural filename order. Container metadata is
preferred over audio-stream metadata, and a missing disc number defaults to
disc 1. If any playable file lacks a valid track number, the entire folder uses
case-insensitive natural filename order, so `track2.mp3` precedes
`track10.mp3`.

M3U files are read as UTF-8 with an optional byte-order mark. Blank lines and
metadata or comment lines beginning with `#` are ignored. Relative paths are
resolved from the playlist's directory, and absolute local paths are accepted.
Playlist order is authoritative and is not changed by track metadata or filename
sorting. URL entries and files with unsupported extensions are skipped with a
warning, as are missing, unreadable, invalid, audio-less, or otherwise unplayable
entries. If no playable entries remain, `cla` reports an error and does not
publish a new playback session.

The command produces no output when playback or a mutating control succeeds,
except for first/last-track boundary messages. The `status` and `list` queries
produce the output described above. `cla` validates files with `ffprobe`, launches
audio-only `ffplay` through a detached coordinator, and returns after playback has
started and its control session is ready. Folder tracks play sequentially.
Playback and later controls do not require the originating terminal to remain
open.

Supported formats depend on the installed FFmpeg build. Typical builds support
WAV, MP3, FLAC, OGG/Vorbis, and AAC/M4A. URLs are not supported.

Errors are written to standard error for missing or unreadable paths, folders
without matching files, missing FFmpeg tools, invalid or audio-less media,
validation timeouts, and failures to start playback. During folder or playlist
playback, a bad file produces a warning and later tracks continue. Errors that
occur inside `ffplay` after startup may also appear in the terminal asynchronously.

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

## To-do

- [x] [#20: Accept spaced arguments for `ff` and `rw` commands](https://github.com/tihnessa/cla/issues/20)
- [x] [#19: Extend `cla skip` with absolute and relative track jumps](https://github.com/tihnessa/cla/issues/19)
- [x] [#18: Add `cla list` command to display the current playlist](https://github.com/tihnessa/cla/issues/18)
- [x] [#17: Add `cla add` command to append tracks to the current playlist](https://github.com/tihnessa/cla/issues/17)
