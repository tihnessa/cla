import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from cla.cli import main
from cla.session import CONTROL_COMMANDS, ControlResponse
from cla.worker import PlaybackController, Track

EXPECTED_CONTROL_COMMANDS = {
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
        Track(Path("track1.mp3"), track=1, disc=1, duration=20.0),
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


def test_rewind_clamps_and_preserves_paused_state(controller) -> None:
    player, now, processes = controller
    now[0] += 4
    player.handle("pause")

    assert player.handle("rw").ok
    assert player.offset == 0
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

    assert player.handle("ff").ok
    assert player.index == 1
    assert player.offset == 0
    assert processes[-1][0][-1] == "track2.mp3"

    now[0] += 25
    assert player.handle("ff").ok
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


def test_player_process_uses_detached_stream_options(controller) -> None:
    _, _, processes = controller
    _, options, _ = processes[0]
    assert options == {
        "close_fds": True,
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
    }


def test_public_control_commands_use_rw_and_exclude_rew() -> None:
    assert CONTROL_COMMANDS == EXPECTED_CONTROL_COMMANDS


@pytest.mark.parametrize("command", sorted(EXPECTED_CONTROL_COMMANDS))
def test_bare_control_command_wins_over_a_colliding_file(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / command).touch()
    monkeypatch.chdir(tmp_path)
    request = Mock(return_value=ControlResponse(True))
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
