"""Renders a lead's persisted `lead_traces` row (app/db.py) into readable text — every
supervisor turn, lane dispatch, researcher tool call, compress output, and the final
verdict pass, in chronological order. The DB row keeps full untruncated content; this
view truncates long fields for readability (raw tool results, page content) since a
single fetch_page call can carry tens of thousands of characters."""
from __future__ import annotations

import json
from typing import Any

_MAX_FIELD_LEN = 400


def _truncate(value: Any, limit: int = _MAX_FIELD_LEN) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated, {len(text)} chars total]"


def _format_tool_calls(tool_calls: list[dict[str, Any]]) -> str:
    if not tool_calls:
        return ""
    parts = []
    for tc in tool_calls:
        args = ", ".join(f"{k}={v!r}" for k, v in tc.get("args", {}).items())
        parts.append(f"{tc['name']}({args})")
    return " | tool_calls: " + ", ".join(parts)


def _format_event(event: dict[str, Any]) -> str:
    phase = event.get("phase", "?")
    kind = event.get("event", "?")
    lane = event.get("lane")
    tag = f"[{phase}" + (f"/{lane}]" if lane else "]")

    if kind == "ai_message":
        content = _truncate(event.get("content") or "(no text)")
        tool_calls = _format_tool_calls(event.get("tool_calls", []))
        iteration = event.get("iteration")
        prefix = f"turn {iteration}: " if iteration is not None else ""
        return f"{tag} {prefix}{content}{tool_calls}"

    if kind == "think":
        return f"{tag} THINK: {_truncate(event.get('reflection', ''))}"

    if kind == "dispatch":
        return f"{tag} DISPATCH lane={event.get('lane')} instructions={_truncate(event.get('instructions', ''))}"

    if kind == "dispatch_skipped":
        return f"{tag} DISPATCH SKIPPED lane={event.get('lane')} (already dispatched {event.get('already_dispatched')}x)"

    if kind == "tool_call":
        args = ", ".join(f"{k}={v!r}" for k, v in event.get("args", {}).items())
        result = event.get("result", {})
        error = result.get("error") if isinstance(result, dict) else None
        result_str = f"ERROR: {error}" if error else _truncate(result)
        return f"{tag} TOOL {event.get('tool')}({args})\n    -> {result_str}"

    if kind == "compress_output":
        claims = event.get("claims", [])
        lines = [f"{tag} COMPRESS ({len(claims)} claim(s), attempt {event.get('attempt', 0) + 1}):"]
        for c in claims:
            lines.append(
                f"    - {c['question_id']}: {c['status']} ({c['confidence']}) — {_truncate(c['answer'], 200)}"
            )
        return "\n".join(lines)

    if kind == "compress_parse_failed":
        return f"{tag} COMPRESS FAILED (fell back to uncompressed/could_not_verify)"

    if kind == "lane_complete":
        return (
            f"{tag} LANE COMPLETE status={event.get('lane_status')} "
            f"claims={event.get('claim_count')}"
        )

    if kind == "lane_timeout":
        return f"{tag} LANE TIMED OUT after {event.get('timeout_seconds')}s"

    if kind == "lane_crash":
        return f"{tag} LANE CRASHED: {event.get('error')}"

    if kind == "research_complete_blocked":
        return f"{tag} research_complete BLOCKED — unanswered HARD gates: {event.get('unanswered')}"

    if kind == "research_complete_accepted":
        return f"{tag} research_complete ACCEPTED (budget_exhausted={event.get('budget_exhausted')})"

    if kind == "code_gate_reject":
        return f"{tag} CODE-GATE REJECT — reason_code={event.get('reason_code')}"

    if kind == "verdict_output":
        return (
            f"{tag} VERDICT: {event.get('final_verdict')} "
            f"(llm said {event.get('llm_verdict')}, force_low={event.get('force_low')})\n"
            f"    rationale: {_truncate(event.get('rationale', ''), 500)}"
        )

    return f"{tag} {kind}: {_truncate({k: v for k, v in event.items() if k not in ('phase', 'event', 'ts')})}"


def format_trace(entity_id: str, canonical_name: str, trace: list[dict[str, Any]]) -> str:
    lines = [f"===== TRACE: {entity_id}  {canonical_name!r} =====", f"({len(trace)} events)", ""]
    for event in trace:
        lines.append(_format_event(event))
    return "\n".join(lines)
