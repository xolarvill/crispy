import pytest

from app.agents.board_tool_protocol import (
    ToolProtocolError,
    extract_json_object,
    parse_tool_turn,
)


def test_parses_fenced_json_and_tool_calls():
    turn = parse_tool_turn(
        '```json\n{"summary": "placed", "tool_calls": [{"name": "create_note", '
        '"args": {"text": "hi"}}]}\n```'
    )
    assert turn.summary == "placed"
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == "create_note"
    assert turn.tool_calls[0].args == {"text": "hi"}
    assert turn.final is False


def test_parses_json_wrapped_in_prose():
    data = extract_json_object('Here is my answer:\n{"summary": "ok", "tool_calls": []}\nThanks')
    assert data["summary"] == "ok"


def test_empty_tool_calls_finish_the_pass():
    turn = parse_tool_turn('{"summary": "nothing to add"}')
    assert turn.tool_calls == []
    assert turn.final is True


def test_tolerates_single_call_and_string_shorthand():
    single = parse_tool_turn('{"tool_calls": {"name": "get_board", "args": {}}}')
    assert single.tool_calls[0].name == "get_board"
    shorthand = parse_tool_turn('{"tool_calls": ["get_board"]}')
    assert shorthand.tool_calls[0].args == {}


def test_rejects_unusable_answers():
    with pytest.raises(ToolProtocolError):
        parse_tool_turn("")
    with pytest.raises(ToolProtocolError):
        parse_tool_turn("I cannot help with that.")
    with pytest.raises(ToolProtocolError):
        parse_tool_turn('{"tool_calls": [{"args": {}}]}')
    with pytest.raises(ToolProtocolError):
        parse_tool_turn('{"tool_calls": [{"name": "x", "args": []}]}')
    with pytest.raises(ToolProtocolError):
        parse_tool_turn('["not", "an", "object"]')
