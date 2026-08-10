from pathlib import Path
from unittest.mock import Mock

import pytest

from cla.cli import (
    ProbeResult,
    _directory_sources,
    _playlist_candidates,
    _write_manifest,
    main,
)
from cla.session import ControlResponse, SessionDescriptor
from cla.worker import PlaybackController, Track, _read_manifest, worker_main


def _install_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = {"ffplay": "/tools/ffplay", "ffprobe": "/tools/ffprobe"}
    monkeypatch.setattr("cla.cli.shutil.which", tools.get)


def test_playlist_parsing_ignores_metadata_and_resolves_local_paths(
    tmp_path: Path,
) -> None:
    relative = tmp_path / "music" / "relative.mp3"
    relative.parent.mkdir()
    relative.touch()
    absolute = tmp_path / "absolute.FLAC"
    absolute.touch()
    playlist = tmp_path / "mix.M3U"
    playlist.write_text(
        "\ufeff#EXTM3U\n\n#EXTINF:1,Relative\n music/relative.mp3 \n"
        f"# a comment\n{absolute}\n",
        encoding="utf-8",
    )

    candidates, error = _playlist_candidates(playlist)

    assert error is None
    assert candidates == [relative.resolve(), absolute.resolve()]


def test_playlist_warns_and_skips_urls_and_unsupported_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    playlist = tmp_path / "mix.m3u"
    playlist.write_text(
        "https://example.com/song.mp3\nhttps://[invalid/song.mp3\n"
        "cover.jpg\nlocal.mp3\n",
        encoding="utf-8",
    )

    candidates, error = _playlist_candidates(playlist)

    assert error is None
    assert candidates == [(tmp_path / "local.mp3").resolve()]
    stderr = capsys.readouterr().err
    assert stderr.count("URL entries are not supported") == 2
    assert "unsupported audio type" in stderr


def test_playlist_with_no_local_audio_entries_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    playlist = tmp_path / "empty.m3u"
    playlist.write_text("#EXTM3U\nhttps://example.com/song.mp3\n", encoding="utf-8")

    candidates, error = _playlist_candidates(playlist)

    assert candidates == []
    assert error is not None
    assert "no supported local audio entries" in error
    assert "URL entries" in capsys.readouterr().err


def test_unreadable_or_non_utf8_playlist_is_rejected(
    tmp_path: Path,
) -> None:
    playlist = tmp_path / "broken.m3u"
    playlist.write_bytes(b"\xff\xfe\x00")

    candidates, error = _playlist_candidates(playlist)

    assert candidates == []
    assert error is not None
    assert "could not read playlist" in error


def test_folder_discovery_is_non_recursive_and_case_insensitive(
    tmp_path: Path,
) -> None:
    (tmp_path / "track.mp3").touch()
    (tmp_path / "B.M3U").touch()
    (tmp_path / "a.m3u").touch()
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "hidden.mp3").touch()
    (nested / "hidden.m3u").touch()

    sources, error = _directory_sources(tmp_path)

    assert error is None
    assert [path.name for path in sources.audio_files] == ["track.mp3"]
    assert [path.name for path in sources.playlists] == ["a.m3u", "B.M3U"]


def test_direct_playlist_starts_worker_with_authoritative_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "track10.mp3"
    second = tmp_path / "track2.mp3"
    first.touch()
    second.touch()
    playlist = tmp_path / "mix.M3U"
    playlist.write_text("track10.mp3\ntrack2.mp3\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    write_manifest = Mock(return_value=manifest)
    _install_tools(monkeypatch)
    monkeypatch.setattr("cla.cli._write_manifest", write_manifest)
    monkeypatch.setattr("cla.cli._start_worker", Mock(return_value=None))

    assert main([str(playlist)]) == 0

    assert write_manifest.call_args.args == (
        [first.resolve(), second.resolve()],
        "/tools/ffprobe",
        "/tools/ffplay",
    )
    assert write_manifest.call_args.kwargs == {"input_order_authoritative": True}


def test_direct_playlist_add_preserves_authoritative_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "track10.mp3"
    second = tmp_path / "track2.mp3"
    first.touch()
    second.touch()
    playlist = tmp_path / "mix.M3U"
    playlist.write_text("track10.mp3\ntrack2.mp3\n", encoding="utf-8")
    manifest = tmp_path / "append.json"
    _install_tools(monkeypatch)
    monkeypatch.setattr(
        "cla.cli.read_session",
        Mock(return_value=SessionDescriptor(43210, "secret", 1234)),
    )
    write_manifest = Mock(return_value=manifest)
    append = Mock(return_value=ControlResponse(True))
    monkeypatch.setattr("cla.cli._write_manifest", write_manifest)
    monkeypatch.setattr("cla.cli.send_append", append)

    assert main(["add", str(playlist)]) == 0

    assert write_manifest.call_args.args == (
        [first.resolve(), second.resolve()],
        "/tools/ffprobe",
        "/tools/ffplay",
    )
    assert write_manifest.call_args.kwargs == {"input_order_authoritative": True}
    append.assert_called_once_with(manifest, candidate_count=2)


@pytest.mark.parametrize(
    ("audio_names", "playlist_names", "answers", "selected"),
    [
        ([], ["only.m3u"], [], "only.m3u"),
        ([], ["b.m3u", "a.m3u"], ["2"], "b.m3u"),
        (["track.mp3"], ["mix.m3u"], ["1"], "track.mp3"),
        (["track.mp3"], ["mix.m3u"], ["2"], "mix.m3u"),
        (["track.mp3"], ["b.m3u", "a.m3u"], ["2", "1"], "a.m3u"),
    ],
)
def test_folder_source_selection_branches(
    audio_names: list[str],
    playlist_names: list[str],
    answers: list[str],
    selected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in audio_names:
        (tmp_path / name).touch()
    playlist_track = tmp_path / "media" / "playlist-track.mp3"
    playlist_track.parent.mkdir()
    playlist_track.touch()
    for name in playlist_names:
        (tmp_path / name).write_text("media/playlist-track.mp3\n", encoding="utf-8")
    responses = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))
    _install_tools(monkeypatch)
    write_manifest = Mock(return_value=tmp_path / "manifest.json")
    monkeypatch.setattr("cla.cli._write_manifest", write_manifest)
    monkeypatch.setattr("cla.cli._start_worker", Mock(return_value=None))

    assert main([str(tmp_path)]) == 0

    files = write_manifest.call_args.args[0]
    if selected.endswith(".m3u"):
        assert files == [playlist_track.resolve()]
        assert write_manifest.call_args.kwargs == {"input_order_authoritative": True}
    else:
        assert [path.name for path in files] == [selected]
        assert write_manifest.call_args.kwargs == {}


@pytest.mark.parametrize("answer", ["", "9", "q"])
def test_invalid_or_cancelled_selection_does_not_replace_active_session(
    answer: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "a.m3u").touch()
    (tmp_path / "b.m3u").touch()
    monkeypatch.setattr("builtins.input", Mock(return_value=answer))
    stop = Mock()
    monkeypatch.setattr("cla.cli._stop_existing_session", stop)

    assert main([str(tmp_path)]) == 1

    stop.assert_not_called()
    assert "selection" in capsys.readouterr().err


def test_cancelled_add_does_not_contact_or_replace_active_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "a.m3u").touch()
    (tmp_path / "b.m3u").touch()
    monkeypatch.setattr("builtins.input", Mock(return_value="q"))
    append = Mock()
    stop = Mock()
    monkeypatch.setattr("cla.cli.send_append", append)
    monkeypatch.setattr("cla.cli._stop_existing_session", stop)

    assert main(["add", str(tmp_path)]) == 1

    append.assert_not_called()
    stop.assert_not_called()
    assert "selection cancelled" in capsys.readouterr().err


@pytest.mark.parametrize("failure", [EOFError, KeyboardInterrupt])
def test_unavailable_interactive_input_does_not_replace_active_session(
    failure: type[BaseException],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "a.m3u").touch()
    (tmp_path / "b.m3u").touch()
    monkeypatch.setattr("builtins.input", Mock(side_effect=failure))
    stop = Mock()
    monkeypatch.setattr("cla.cli._stop_existing_session", stop)

    assert main([str(tmp_path)]) == 1

    stop.assert_not_called()
    assert "cancelled" in capsys.readouterr().err


def test_manifest_round_trip_includes_authoritative_order(tmp_path: Path) -> None:
    files = [tmp_path / "second.mp3", tmp_path / "first.mp3"]
    manifest = _write_manifest(
        files,
        "/tools/ffprobe",
        "/tools/ffplay",
        input_order_authoritative=True,
    )

    assert _read_manifest(manifest) == (
        files,
        "/tools/ffprobe",
        "/tools/ffplay",
        True,
    )


def test_controller_preserves_authoritative_playlist_order() -> None:
    tracks = [
        Track(Path("track10.mp3"), track=10, disc=1, duration=1),
        Track(Path("track2.mp3"), track=2, disc=1, duration=1),
    ]

    controller = PlaybackController(
        "/tools/ffplay", tracks, input_order_authoritative=True
    )

    assert [track.path for track in controller.tracks] == [
        Path("track10.mp3"),
        Path("track2.mp3"),
    ]


def test_worker_preserves_order_after_skipping_invalid_playlist_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "track10.mp3"
    bad = tmp_path / "bad.mp3"
    second = tmp_path / "track2.mp3"
    for path in [first, bad, second]:
        path.touch()
    manifest = _write_manifest(
        [first, bad, second],
        "/tools/ffprobe",
        "/tools/ffplay",
        input_order_authoritative=True,
    )
    monkeypatch.setattr(
        "cla.worker._probe_audio",
        Mock(
            side_effect=[
                ProbeResult(track=10, duration=1),
                ProbeResult(error="invalid"),
                ProbeResult(track=2, duration=1),
            ]
        ),
    )
    serve = Mock(return_value=0)
    monkeypatch.setattr("cla.worker._serve", serve)

    assert worker_main([str(manifest), str(tmp_path / "startup.json")]) == 0
    assert [track.path for track in serve.call_args.args[0].tracks] == [first, second]
