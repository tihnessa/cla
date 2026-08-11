import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from cla.cli import main
from cla.session import (
    CONTROL_COMMANDS,
    ControlResponse,
    PlaybackStatus,
    QueueSnapshot,
    SessionDescriptor,
    parse_skip_command,
)
from cla.worker import PlaybackController, Track

EXPECTED_CONTROL_COMMANDS = {
    "kill",
    "list",
    "pause",
    "play",
    "skip",
    "next",
    "back",
    "prev",
    "ff",
    "rw",
    "replay",
    "restart",
    "status",
}


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture
def controller():
    now = [100.0]
    processes = []

    def popen(command, **options):
        process = FakeProcess()
        processes.append((command, options, process))
        return process

    tracks = [
        Track(Path("track2.mp3"), track=2, disc=1, duration=30.0),
        Track(
            Path("track1.mp3"),
            track=1,
            disc=1,
            duration=20.0,
            title="First track",
        ),
    ]
    player = PlaybackController(
        "/tools/ffplay",
        tracks,
        clock=lambda: now[0],
        popen=popen,
    )
    assert player.start().ok
    return player, now, processes


def test_navigation_uses_ordered_snapshot_and_aliases(controller) -> None:
    player, _, processes = controller

    assert processes[0][0][-1] == "track1.mp3"
    assert player.handle("skip").ok
    assert processes[-1][0][-1] == "track2.mp3"
    boundary = player.handle("next")
    assert boundary.ok
    assert boundary.message == "already on the last track"

    assert player.handle("back").ok
    assert processes[-1][0][-1] == "track1.mp3"
    boundary = player.handle("prev")
    assert boundary.ok
    assert boundary.message == "already on the first track"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("skip", ("next", 1)),
        ("skip12", ("absolute", 12)),
        ("skip+3", ("relative", 3)),
        ("skip-2", ("relative", -2)),
    ],
)
def test_skip_parser_accepts_bare_absolute_and_relative_forms(
    command: str, expected: tuple[str, int]
) -> None:
    assert parse_skip_command(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "skip0",
        "skip+0",
        "skip-0",
        "skip+",
        "skip-",
        "skip--2",
        "skip++2",
        "skipabc",
        "skip1.5",
        "skip１２",
        "skip" + "9" * 5000,
    ],
)
def test_skip_parser_rejects_invalid_or_oversized_forms(command: str) -> None:
    assert parse_skip_command(command) is None


def _jump_controller() -> tuple[PlaybackController, list[FakeProcess]]:
    processes = []

    def popen(*args, **kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    player = PlaybackController(
        "/tools/ffplay",
        [
            Track(Path(f"track{index}.mp3"), track=index, disc=1, duration=60.0)
            for index in range(1, 5)
        ],
        input_order_authoritative=True,
        popen=popen,
    )
    assert player.start().ok
    return player, processes


def test_absolute_and_relative_skip_jumps_select_from_zero() -> None:
    player, processes = _jump_controller()

    assert player.handle("skip3").ok
    assert player.index == 2
    assert player.offset == 0
    assert processes[-1] is player.process

    player.handle("pause")
    assert player.handle("skip-2").ok
    assert player.index == 0
    assert player.offset == 0
    assert not player.paused

    assert player.handle("skip+3").ok
    assert player.index == 3


def test_absolute_skip_to_current_track_restarts_it() -> None:
    player, processes = _jump_controller()
    assert player.handle("skip3").ok
    current_process = player.process

    assert player.handle("skip3").ok

    assert current_process is not None
    assert current_process.terminated
    assert player.index == 2
    assert player.offset == 0
    assert player.process is processes[-1]
    assert player.process is not current_process


@pytest.mark.parametrize("command", ["skip5", "skip-1", "skip+4"])
def test_out_of_range_skip_leaves_playback_unchanged(command: str) -> None:
    player, processes = _jump_controller()
    process = player.process

    response = player.handle(command)

    assert response == ControlResponse(True, "value is out of range")
    assert player.index == 0
    assert player.offset == 0
    assert not player.paused
    assert player.process is process
    assert process is not None
    assert not process.terminated
    assert len(processes) == 1


def test_out_of_range_relative_skip_from_final_track_does_not_wrap() -> None:
    player, processes = _jump_controller()
    assert player.handle("skip4").ok
    process = player.process

    response = player.handle("skip+1")

    assert response == ControlResponse(True, "value is out of range")
    assert player.index == 3
    assert player.process is process
    assert process is not None
    assert not process.terminated
    assert len(processes) == 2


def test_invalid_joined_skip_is_a_silent_worker_no_op() -> None:
    player, processes = _jump_controller()
    process = player.process

    assert player.handle("skip0") == ControlResponse(True)
    assert player.index == 0
    assert player.process is process
    assert process is not None
    assert not process.terminated
    assert len(processes) == 1


def test_pause_and_play_are_distinct_silent_idempotent_operations(controller) -> None:
    player, now, processes = controller
    now[0] += 7

    assert player.handle("pause").message is None
    assert player.paused
    assert player.offset == 7
    count = len(processes)
    assert player.handle("pause").ok
    assert len(processes) == count

    assert player.handle("play").message is None
    assert not player.paused
    assert "-ss" in processes[-1][0]
    assert "7" in processes[-1][0]
    count = len(processes)
    assert player.handle("play").ok
    assert len(processes) == count


def test_status_is_read_only_and_uses_authoritative_position(controller) -> None:
    player, now, processes = controller
    now[0] += 7.9

    response = player.handle("status")

    assert response.ok
    assert response.status is not None
    assert response.status.title == "First track"
    assert response.status.elapsed == pytest.approx(7.9)
    assert response.status.duration == 20.0
    assert player.index == 0
    assert player.offset == 0
    assert not player.paused
    assert len(processes) == 1

    player.handle("pause")
    paused = player.handle("status")
    now[0] += 50

    assert player.handle("status") == paused
    assert paused.status is not None
    assert paused.status.elapsed == pytest.approx(7.9)


def test_status_falls_back_to_filename_and_clamps_elapsed(controller) -> None:
    player, now, _ = controller
    player.tracks[1] = Track(Path("private/track2.mp3"), track=2, disc=1, duration=30.0)
    player.handle("next")
    now[0] += 40

    response = player.handle("status")

    assert response.status == PlaybackStatus("track2.mp3", elapsed=30.0, duration=30.0)


def test_list_is_an_ordered_read_only_snapshot_with_public_labels(controller) -> None:
    player, _, processes = controller

    response = player.handle("list")

    assert response.queue == QueueSnapshot(("First track", "track2.mp3"), 1)
    assert player.index == 0
    assert player.offset == 0
    assert not player.paused
    assert len(processes) == 1

    assert player.handle("next").ok
    player.tracks[1] = Track(Path("private/track2.mp3"), track=2, disc=1, duration=30.0)

    assert player.handle("list").queue == QueueSnapshot(
        ("First track", "track2.mp3"), 2
    )


def test_rewind_clamps_and_preserves_paused_state(controller) -> None:
    player, now, processes = controller
    now[0] += 4
    player.handle("pause")

    assert player.handle("rw").ok
    assert player.offset == 0
    assert player.paused
    assert len(processes) == 1


def test_custom_seek_uses_requested_positive_whole_seconds(controller) -> None:
    player, now, processes = controller
    now[0] += 12
    player.handle("pause")

    assert player.handle("rw7").ok
    assert player.offset == 5
    assert player.paused
    assert len(processes) == 1

    assert player.handle("rw20").ok
    assert player.offset == 0

    assert player.handle("ff19").ok
    assert player.offset == 19
    assert player.paused
    assert len(processes) == 1


@pytest.mark.parametrize(
    ("direction", "seconds", "expected_offset"),
    [("rw", "7", 5), ("ff", "7", 19)],
)
def test_spaced_seek_uses_compact_controller_behavior(
    direction: str,
    seconds: str,
    expected_offset: int,
    controller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player, now, processes = controller
    now[0] += 12
    player.handle("pause")
    monkeypatch.setattr("cla.cli.send_command", player.handle)

    assert main([direction, seconds]) == 0
    assert player.offset == expected_offset
    assert player.paused
    assert len(processes) == 1


@pytest.mark.parametrize("command", ["ff0", "rw-5", "ffabc", "rw1.5"])
def test_invalid_custom_seek_is_a_no_op(command: str, controller) -> None:
    player, now, processes = controller
    now[0] += 6
    player.handle("pause")

    assert player.handle(command).ok
    assert player.offset == 6
    assert player.index == 0
    assert player.paused
    assert len(processes) == 1


def test_removed_rew_command_is_rejected_by_worker(controller) -> None:
    player, _, _ = controller

    response = player.handle("rew")

    assert not response.ok
    assert response.message == "unknown playback command"


def test_fast_forward_crosses_tracks_and_stops_after_final_track(controller) -> None:
    player, now, processes = controller
    now[0] += 15

    assert player.handle("ff5").ok
    assert player.index == 1
    assert player.offset == 0
    assert processes[-1][0][-1] == "track2.mp3"

    now[0] += 25
    assert player.handle("ff5").ok
    assert player.stopped


def test_replay_and_restart_begin_playing_from_zero(controller) -> None:
    player, now, processes = controller
    player.handle("next")
    now[0] += 5
    player.handle("pause")

    assert player.handle("replay").ok
    assert player.index == 1
    assert player.offset == 0
    assert not player.paused

    player.handle("pause")
    assert player.handle("restart").ok
    assert player.index == 0
    assert player.offset == 0
    assert not player.paused
    assert processes[-1][0][-1] == "track1.mp3"


def test_single_track_restart_and_replay_are_equivalent() -> None:
    processes = []

    def popen(*args, **kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    player = PlaybackController(
        "/tools/ffplay",
        [Track(Path("only.mp3"), track=1, disc=1, duration=60.0)],
        popen=popen,
    )
    player.start()

    assert player.handle("replay").ok
    assert player.handle("restart").ok
    assert player.index == 0
    assert player.offset == 0
    assert len(processes) == 3


def test_paused_fast_forward_preserves_pause_until_track_boundary(controller) -> None:
    player, now, processes = controller
    now[0] += 5
    player.handle("pause")

    assert player.handle("ff").ok
    assert player.offset == 15
    assert player.paused
    assert len(processes) == 1

    assert player.handle("ff").ok
    assert player.index == 1
    assert player.offset == 0
    assert not player.paused
    assert len(processes) == 2


def test_natural_completion_advances_but_intentional_stop_does_not(controller) -> None:
    player, _, processes = controller
    processes[0][2].returncode = 0

    player.tick()

    assert player.index == 1
    assert len(processes) == 2
    assert player.handle("replay").ok
    player.tick()
    assert player.index == 1


def test_append_extends_the_queue_without_interrupting_playback(controller) -> None:
    player, _, processes = controller
    current_process = player.process

    response = player.append(
        [Track(Path("track10.mp3"), track=10, disc=1, duration=40.0)]
    )

    assert response == ControlResponse(True)
    assert player.process is current_process
    assert [track.path.name for track in player.tracks] == [
        "track1.mp3",
        "track2.mp3",
        "track10.mp3",
    ]
    assert len(processes) == 1


def test_append_orders_only_the_new_batch(controller) -> None:
    player, _, _ = controller

    assert player.append(
        [
            Track(Path("added10.mp3"), track=None, disc=None, duration=40.0),
            Track(Path("added2.mp3"), track=None, disc=None, duration=40.0),
        ]
    ).ok

    assert [track.path.name for track in player.tracks] == [
        "track1.mp3",
        "track2.mp3",
        "added2.mp3",
        "added10.mp3",
    ]


def test_append_after_natural_completion_starts_first_added_track(controller) -> None:
    player, _, processes = controller
    player.index = len(player.tracks) - 1
    player.process = processes[-1][2]
    player.process.returncode = 0
    player.tick()

    assert player.stopped
    assert player.append([Track(Path("new.mp3"), track=1, disc=1, duration=40.0)]).ok

    assert not player.stopped
    assert player.index == 2
    assert processes[-1][0][-1] == "new.mp3"


def test_failed_restart_of_finished_queue_rolls_back_append(controller) -> None:
    player, _, processes = controller
    player.index = len(player.tracks) - 1
    player.process = processes[-1][2]
    player.process.returncode = 0
    player.tick()
    original_tracks = list(player.tracks)
    player.popen = Mock(side_effect=OSError("cannot execute"))

    response = player.append([Track(Path("new.mp3"), track=1, disc=1, duration=40.0)])

    assert not response.ok
    assert player.tracks == original_tracks
    assert player.index == 1
    assert player.stopped


def test_immediate_exit_when_reviving_finished_queue_rolls_back_append(
    controller,
) -> None:
    player, _, processes = controller
    player.index = len(player.tracks) - 1
    player.process = processes[-1][2]
    player.process.returncode = 0
    player.tick()
    original_tracks = list(player.tracks)
    immediate_exit = FakeProcess()
    immediate_exit.returncode = 1
    player.popen = Mock(return_value=immediate_exit)

    response = player.append([Track(Path("new.mp3"), track=1, disc=1, duration=40.0)])

    assert not response.ok
    assert response.message == "ffplay exited with status 1 during startup"
    assert player.tracks == original_tracks
    assert player.index == 1
    assert player.process is None
    assert player.stopped


def test_append_after_launch_failure_retries_existing_queue_before_additions(
    controller,
) -> None:
    player, _, processes = controller
    processes[0][2].returncode = 0
    player.popen = Mock(side_effect=[OSError("temporary failure"), FakeProcess()])

    player.tick()

    assert player.index == 1
    assert player.stopped
    response = player.append([Track(Path("new.mp3"), track=1, disc=1, duration=40.0)])

    assert response.ok
    assert player.index == 1
    assert player.current.path == Path("track2.mp3")
    assert not player.stopped
    assert [track.path.name for track in player.tracks] == [
        "track1.mp3",
        "track2.mp3",
        "new.mp3",
    ]


def test_append_after_failed_final_track_retries_it_before_additions(
    controller,
) -> None:
    player, _, processes = controller
    player.index = len(player.tracks) - 1
    player.process = processes[-1][2]
    player.process.returncode = 1
    player.tick()

    assert player.stopped
    assert player.append([Track(Path("new.mp3"), track=1, disc=1)]).ok

    assert player.index == 1
    assert player.current.path == Path("track2.mp3")
    assert processes[-1][0][-1] == "track2.mp3"


def test_kill_terminates_playback_without_advancing_playlist(controller) -> None:
    player, _, processes = controller
    process = processes[0][2]

    response = player.handle("kill")
    player.tick()

    assert response == ControlResponse(True)
    assert process.terminated
    assert player.process is None
    assert player.stopped
    assert player.index == 0
    assert len(processes) == 1


def test_player_process_uses_detached_stream_options(controller) -> None:
    _, _, processes = controller
    _, options, _ = processes[0]
    assert options == {
        "close_fds": True,
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
    }


def test_public_control_commands_include_kill_and_exclude_rew() -> None:
    assert CONTROL_COMMANDS == EXPECTED_CONTROL_COMMANDS


@pytest.mark.parametrize("command", sorted(EXPECTED_CONTROL_COMMANDS))
def test_bare_control_command_wins_over_a_colliding_file(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / command).touch()
    monkeypatch.chdir(tmp_path)
    response = (
        ControlResponse(
            True,
            status=PlaybackStatus("Song", elapsed=0.0, duration=1.0),
        )
        if command == "status"
        else (
            ControlResponse(True, queue=QueueSnapshot(("Song",), 1))
            if command == "list"
            else ControlResponse(True)
        )
    )
    request = Mock(return_value=response)
    monkeypatch.setattr("cla.cli.send_command", request)

    assert main([command]) == 0
    request.assert_called_once_with(command)


def test_bare_control_command_wins_over_a_colliding_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "next").mkdir()
    monkeypatch.chdir(tmp_path)
    request = Mock(return_value=ControlResponse(True))
    monkeypatch.setattr("cla.cli.send_command", request)

    assert main(["next"]) == 0
    request.assert_called_once_with("next")


@pytest.mark.parametrize("command", ["ff20", "rw30"])
def test_custom_seek_command_wins_over_a_colliding_file(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / command).touch()
    monkeypatch.chdir(tmp_path)
    request = Mock(return_value=ControlResponse(True))
    monkeypatch.setattr("cla.cli.send_command", request)

    assert main([command]) == 0
    request.assert_called_once_with(command)


@pytest.mark.parametrize(
    ("compact", "spaced"),
    [
        ("ff30", ["ff", "30"]),
        ("rw30", ["rw", "30"]),
        ("skip12", ["skip", "12"]),
        ("skip+3", ["skip", "+3"]),
        ("skip-2", ["skip", "-2"]),
    ],
)
def test_compact_and_spaced_controls_send_the_same_normalized_command(
    compact: str,
    spaced: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = Mock(return_value=ControlResponse(True))
    monkeypatch.setattr("cla.cli.send_command", request)

    assert main([compact]) == 0
    request.assert_called_once_with(compact)

    request.reset_mock()
    assert main(spaced) == 0
    request.assert_called_once_with(compact)


@pytest.mark.parametrize(("value", "expected_index"), [("3", 2), ("+3", 3)])
def test_spaced_skip_uses_compact_controller_behavior(
    value: str,
    expected_index: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player, _ = _jump_controller()
    monkeypatch.setattr("cla.cli.send_command", player.handle)

    assert main(["skip", value]) == 0
    assert player.index == expected_index


@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("ff", "0"),
        ("rw", "-5"),
        ("ff", "abc"),
        ("rw", "1.5"),
        ("ff", "１２"),
        ("ff", "--2"),
        ("ff", ""),
        ("rw", ""),
        ("skip", "0"),
        ("skip", "+0"),
        ("skip", "-0"),
        ("skip", "+"),
        ("skip", "--2"),
        ("skip", "abc"),
        ("skip", ""),
        ("skip", "9" * 5000),
    ],
)
def test_invalid_spaced_control_is_ignored_before_filesystem_handling(
    target: str,
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if len(value) < 100:
        (tmp_path / value).touch()
    monkeypatch.chdir(tmp_path)
    request = Mock()
    tools = Mock(side_effect=AssertionError("filesystem handling was reached"))
    monkeypatch.setattr("cla.cli.send_command", request)
    monkeypatch.setattr("cla.cli._tools", tools)

    assert main([target, value]) == 0
    request.assert_not_called()
    tools.assert_not_called()


@pytest.mark.parametrize(
    ("target", "value", "normalized"),
    [
        ("ff", "20", "ff20"),
        ("rw", "30", "rw30"),
        ("skip", "2", "skip2"),
        ("skip", "+3", "skip+3"),
        ("skip", "-1", "skip-1"),
    ],
)
def test_spaced_control_wins_over_colliding_files(
    target: str,
    value: str,
    normalized: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / target).touch()
    (tmp_path / value).touch()
    (tmp_path / normalized).touch()
    monkeypatch.chdir(tmp_path)
    request = Mock(return_value=ControlResponse(True))
    monkeypatch.setattr("cla.cli.send_command", request)

    assert main([target, value]) == 0
    request.assert_called_once_with(normalized)


@pytest.mark.parametrize("command", ["skip2", "skip+3", "skip-1"])
def test_valid_joined_skip_command_wins_over_a_colliding_file(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / command).touch()
    monkeypatch.chdir(tmp_path)
    request = Mock(return_value=ControlResponse(True))
    monkeypatch.setattr("cla.cli.send_command", request)

    assert main([command]) == 0
    request.assert_called_once_with(command)


@pytest.mark.parametrize(
    "command",
    ["ff0", "rw0", "ff-5", "rwabc", "ff1.5", "ff" + "9" * 5000],
)
def test_invalid_custom_seek_is_ignored_before_filesystem_handling(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if len(command) < 100:
        (tmp_path / command).touch()
    monkeypatch.chdir(tmp_path)
    request = Mock()
    tools = Mock(side_effect=AssertionError("filesystem handling was reached"))
    monkeypatch.setattr("cla.cli.send_command", request)
    monkeypatch.setattr("cla.cli._tools", tools)

    assert main([command]) == 0
    request.assert_not_called()
    tools.assert_not_called()


@pytest.mark.parametrize(
    "command",
    [
        "skip0",
        "skip+0",
        "skip-0",
        "skip+",
        "skip--2",
        "skipabc",
        "skip" + "9" * 5000,
    ],
)
def test_invalid_joined_skip_is_ignored_before_filesystem_handling(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if len(command) < 100:
        (tmp_path / command).touch()
    monkeypatch.chdir(tmp_path)
    request = Mock()
    tools = Mock(side_effect=AssertionError("filesystem handling was reached"))
    monkeypatch.setattr("cla.cli.send_command", request)
    monkeypatch.setattr("cla.cli._tools", tools)

    assert main([command]) == 0
    request.assert_not_called()
    tools.assert_not_called()


def test_qualified_custom_seek_name_remains_a_filesystem_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "ff20").touch()
    monkeypatch.chdir(tmp_path)
    request = Mock()
    monkeypatch.setattr("cla.cli.send_command", request)
    monkeypatch.setattr(
        "cla.cli._tools", Mock(return_value=(None, None, "filesystem target"))
    )

    assert main(["./ff20"]) == 1
    assert "filesystem target" in capsys.readouterr().err
    request.assert_not_called()


def test_qualified_joined_skip_name_remains_a_filesystem_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "skip2").touch()
    monkeypatch.chdir(tmp_path)
    request = Mock()
    monkeypatch.setattr("cla.cli.send_command", request)
    monkeypatch.setattr(
        "cla.cli._tools", Mock(return_value=(None, None, "filesystem target"))
    )

    assert main(["./skip2"]) == 1
    assert "filesystem target" in capsys.readouterr().err
    request.assert_not_called()


@pytest.mark.parametrize("command", ["next3", "next+3"])
def test_numbered_next_aliases_remain_filesystem_targets(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / command).touch()
    monkeypatch.chdir(tmp_path)
    request = Mock()
    monkeypatch.setattr("cla.cli.send_command", request)
    monkeypatch.setattr(
        "cla.cli._tools", Mock(return_value=(None, None, "filesystem target"))
    )

    assert main([command]) == 1
    assert "filesystem target" in capsys.readouterr().err
    request.assert_not_called()


def test_cli_prints_boundary_and_reports_missing_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "cla.cli.send_command",
        Mock(return_value=ControlResponse(True, "already on the last track")),
    )
    assert main(["next"]) == 0
    assert capsys.readouterr().out == "cla: already on the last track\n"

    monkeypatch.setattr(
        "cla.cli.send_command",
        Mock(return_value=ControlResponse(False, "no active playback session")),
    )
    assert main(["pause"]) == 1
    assert "no active playback session" in capsys.readouterr().err


def test_valid_joined_skip_preserves_missing_session_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = Mock(
        return_value=ControlResponse(
            False, "no active playback session", unavailable=True
        )
    )
    monkeypatch.setattr("cla.cli.send_command", request)

    assert main(["skip2"]) == 1
    assert "no active playback session" in capsys.readouterr().err
    request.assert_called_once_with("skip2")


@pytest.mark.parametrize("command", ["next", "pause", "./ff20"])
def test_non_argument_control_rejects_a_second_value(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = Mock()
    monkeypatch.setattr("cla.cli.send_command", request)

    with pytest.raises(SystemExit) as error:
        main([command, "2"])

    assert error.value.code == 2
    request.assert_not_called()


def test_cli_prints_status_and_treats_an_empty_queue_as_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = Mock(
        return_value=ControlResponse(
            True,
            status=PlaybackStatus("A title", elapsed=83.9, duration=4504.8),
        )
    )
    monkeypatch.setattr("cla.cli.send_command", request)

    assert main(["status"]) == 0
    assert capsys.readouterr() == ("A title — 01:23 / 75:04\n", "")

    request.return_value = ControlResponse(
        False, "no active playback session", unavailable=True
    )
    assert main(["status"]) == 0
    assert capsys.readouterr() == ("Nothing in queue\n", "")


def test_cli_status_preserves_genuine_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "cla.cli.send_command",
        Mock(return_value=ControlResponse(False, "invalid control response")),
    )

    assert main(["status"]) == 1
    assert "invalid control response" in capsys.readouterr().err


def test_cli_kill_waits_for_session_cleanup_and_succeeds_silently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    descriptor = SessionDescriptor(43210, "secret", 1234)
    monkeypatch.setattr(
        "cla.cli.read_session", Mock(side_effect=[descriptor, descriptor, None])
    )
    request = Mock(return_value=ControlResponse(True))
    monkeypatch.setattr("cla.cli.send_command", request)
    monkeypatch.setattr("cla.cli.time.sleep", Mock())

    assert main(["kill"]) == 0
    assert capsys.readouterr() == ("", "")
    request.assert_called_once_with("kill")
