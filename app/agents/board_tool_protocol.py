"""Parser for the JSON tool-turn protocol used by canvas passes.

Providers in this app take one prompt and return text; none of them expose
native tool calls through ``MultimodalChatRequest``. The board pass therefore
asks for a single JSON object::

    {"summary": "one line", "tool_calls": [{"name": "create_note", "args": {...}}]}

Parsing lives here, behind one function, so a future native tool-calling
implementation can replace it without touching the session or the pass.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

MAX_TOOL_CALLS = 24
MAX_SUMMARY = 500

_FENCED = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```$", re.S)


class ToolProtocolError(ValueError):
    """The model's answer is not a usable tool turn."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolTurn:
    summary: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    final: bool = False


def extract_json_object(text: str) -> dict:
    value = (text or "").strip()
    if not value:
        raise ToolProtocolError("the response was empty")
    fenced = _FENCED.match(value)
    if fenced:
        value = fenced.group(1).strip()

    candidates = [value]
    start, end = value.find("{"), value.rfind("}")
    if 0 <= start < end:
        candidates.append(value[start : end + 1])

    parsed = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        break
    if not isinstance(parsed, dict):
        raise ToolProtocolError("the response has no JSON object")
    return parsed


def parse_tool_turn(text: str) -> ToolTurn:
    data = extract_json_object(text)
    summary = str(data.get("summary") or "")[:MAX_SUMMARY]
    raw = data.get("tool_calls")
    if raw is None:
        raw = data.get("calls")
    if raw is None:
        raw = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ToolProtocolError("tool_calls must be a list")

    calls: list[ToolCall] = []
    for index, entry in enumerate(raw[:MAX_TOOL_CALLS]):
        if isinstance(entry, str):
            name, args = entry, {}
        elif isinstance(entry, dict):
            name = entry.get("name") or entry.get("tool")
            args = entry.get("args")
            if args is None:
                args = entry.get("arguments")
            if args is None:
                args = {}
        else:
            raise ToolProtocolError(f"tool_calls[{index}] must be an object")
        if not isinstance(name, str) or not name.strip():
            raise ToolProtocolError(f"tool_calls[{index}] has no name")
        if not isinstance(args, dict):
            raise ToolProtocolError(f"tool_calls[{index}].args must be an object")
        calls.append(ToolCall(name=name.strip(), args=args))

    final = bool(data.get("final")) or not calls
    return ToolTurn(summary=summary, tool_calls=calls, final=final)
