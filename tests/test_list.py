from unittest.mock import Mock

import pytest

from cla.cli import PAGER_PROMPT, _print_queue, _terminal_height, main
from cla.session import ControlResponse, QueueSnapshot


def _queue(length: int, current_index: int = 1) -> QueueSnapshot:
    return QueueSnapshot(
        tuple(f"Track {index}" for index in range(1, length + 1)), current_index
    )


def test_short_queue_prints_all_indices_and_current_marker_without_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("cla.cli._terminal_height", Mock(return_value=3))

    _print_queue(_queue(3, current_index=2))

    assert capsys.readouterr().out == ("1.   Track 1\n2. > Track 2\n3.   Track 3\n")


def test_enter_advances_one_entry_and_q_stops(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("cla.cli._terminal_height", Mock(return_value=4))
    monkeypatch.setattr("cla.cli._read_pager_key", Mock(side_effect=["\n", "q"]))

    _print_queue(_queue(6))

    output = capsys.readouterr().out
    assert "1. > Track 1\n" in output
    assert "3.   Track 3\n" in output
    assert "4.   Track 4\n" in output
    assert "5.   Track 5\n" not in output
    assert output.count(PAGER_PROMPT) == 2


def test_space_advances_by_pages_until_the_queue_is_complete(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("cla.cli._terminal_height", Mock(return_value=4))
    reader = Mock(side_effect=[" ", " "])
    monkeypatch.setattr("cla.cli._read_pager_key", reader)

    _print_queue(_queue(8, current_index=8))

    output = capsys.readouterr().out
    for index in range(1, 9):
        marker = ">" if index == 8 else " "
        assert f"{index}. {marker} Track {index}\n" in output
    assert output.count(PAGER_PROMPT) == 2
    assert reader.call_count == 2


def test_unavailable_input_exits_after_the_first_visible_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("cla.cli._terminal_height", Mock(return_value=4))
    monkeypatch.setattr("cla.cli._read_pager_key", Mock(return_value=None))

    _print_queue(_queue(5))

    output = capsys.readouterr().out
    assert "3.   Track 3\n" in output
    assert "4.   Track 4\n" not in output
    assert output.count(PAGER_PROMPT) == 1


def test_terminal_height_falls_back_to_twenty_on_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cla.cli.shutil.get_terminal_size", Mock(side_effect=OSError("no terminal"))
    )

    assert _terminal_height() == 20


def test_cli_list_treats_no_session_as_an_empty_queue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = Mock(
        return_value=ControlResponse(
            False, "no active playback session", unavailable=True
        )
    )
    monkeypatch.setattr("cla.cli.send_command", request)

    assert main(["list"]) == 0
    assert capsys.readouterr() == ("Nothing in queue\n", "")
    request.assert_called_once_with("list")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (ControlResponse(False, "transport failed"), "transport failed"),
        (ControlResponse(True), "invalid control response"),
    ],
)
def test_cli_list_preserves_errors_and_rejects_a_missing_snapshot(
    response: ControlResponse,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("cla.cli.send_command", Mock(return_value=response))

    assert main(["list"]) == 1
    assert message in capsys.readouterr().err
