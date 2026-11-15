"""Prompt text for a board tool pass.

The pass is deliberately separate from the stage's own persona prompt: it runs
after the stage output is final, annotates the shared canvas, and can never
change the generated result. Keeping the wording in one module makes the
protocol rules reviewable and testable.
"""
from __future__ import annotations

import json

from app.services.board_tools import catalog_json

PROTOCOL_EXAMPLE = """{
  "summary": "Placed the two strongest outputs and wrote one question.",
  "tool_calls": [
    {"name": "place_assets", "args": {"items": [
      {"asset_id": "<id from list_assets>", "caption": "暖色棚拍", "role": "output"}
    ], "link_from": "1"}},
    {"name": "create_note", "args": {"text": "Logos sit 12px from the edge here; safe area is tighter.", "intent": "finding", "near": "2"}}
  ]
}"""

STAGE_HINTS = {
    "planning": (
        "Leave direction and open questions on the canvas: restate the regions in plain words, "
        "explain the chosen angle in a note marked explanation, and ask one question if a region "
        "or the goal is ambiguous. Do not rewrite the operator's goal."
    ),
    "copy_image_generation": (
        "Pin the generated images next to the source or reference they came from, link output → source, "
        "and note which operator regions you applied. Do not claim a region was applied unless the output shows it."
    ),
    "video_scripting": (
        "Pin nothing unless the script references an image on the board; use notes to explain how the "
        "operator's direction shaped the hooks. Ask a question when the direction conflicts with the product."
    ),
    "storyboard_image_generation": (
        "Pin storyboard frames beside their reference frames, link each frame to its source, and "
        "note any operator region you could not honour."
    ),
    "video_generation": (
        "Pin the finished videos or their poster frames beside the storyboard frames they came from, "
        "link output → source, and note any visible deviation from the board direction."
    ),
    "visual_quality_assessment": (
        "Mark a defect as a region with intent=defect on the generated asset and write one finding note per defect. "
        "Keep notes short and specific (what is wrong, where). Do not pin new media unless you compose a comparison."
    ),
    "evaluation_selection": (
        "Compose the top candidates into one labelled comparison sheet, pin it, mark the winner with a note, "
        "and explain the winning reason in one or two sentences. Use compose with pin=true."
    ),
}


def _transcript_text(transcript: list[dict]) -> str:
    if not transcript:
        return "(no rounds yet)"
    text = json.dumps(transcript, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= 9000:
        return text
    return text[:8900] + "… (older rounds trimmed)"


def render_board_tool_prompt(
    *,
    agent_name: str,
    stage_name: str,
    board_digest: str,
    revision: int,
    annotations: dict,
    stage_summary: str,
    round_index: int,
    max_rounds: int,
    transcript: list[dict],
) -> str:
    annotation_text = json.dumps(annotations, ensure_ascii=False, separators=(",", ":"))
    if len(annotation_text) > 3000:
        annotation_text = annotation_text[:2980] + "…"
    hint = STAGE_HINTS.get(stage_name, "Annotate the board only where it helps the operator review this stage.")
    return f"""[BOARD TOOLS — {agent_name} · {stage_name}]

You have just finished the {stage_name} stage. The instruction board is the shared canvas the
operator reviews. You may now read it, annotate it, and pin run media so your result is visible
in context. This pass runs after the stage output is final: it annotates, it does not regenerate.

HARD RULES
1. Human-authored nodes are read-only. Never move, edit or delete operator assets, regions, notes or links.
2. Agent notes from earlier stages are context, not approval. Do not treat them as operator direction.
3. Only state what the stage output and the board actually show. Never invent product facts, scores or assets.
4. If something is ambiguous, create_note with intent="question" and a short, specific question. Questions do not block review.
5. Prefer one batch call over many rounds. Stop as soon as the canvas tells the operator what they need.
6. Coordinates for regions are 0–1 of that asset's own image. Cite assets by their numbered mark (for example "2").
7. No secrets, API keys, internal prompts or chain-of-thought on the canvas.
8. Finish with {{"summary": "...", "tool_calls": []}}. An empty tool list is a valid, preferred answer when the canvas is already clear.

STAGE GUIDANCE
{hint}

STAGE RESULT (summarised)
{stage_summary}

CURRENT BOARD r{revision} — OPERATOR DIGEST
{board_digest or "(the board has no operator content yet)"}

CURRENT ANNOTATIONS (human and agent; human is authoritative)
{annotation_text}

TOOLS (JSON schemas; fields marked required must be present)
{catalog_json()}

PROTOCOL
Reply with exactly one JSON object and nothing else:
{PROTOCOL_EXAMPLE}

PREVIOUS ROUNDS
{_transcript_text(transcript)}

This is round {round_index} of {max_rounds}. Return the next tool calls, or an empty list when finished.
"""
