"""SDK hooks that pipe tool-use events into the coordination DB + transcripts."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .work_queue import WorkQueue

log = logging.getLogger(__name__)


def make_pretool_hook(
    queue: WorkQueue,
    worker_id: int,
    claim_id_ref: dict[str, int | None],
    transcripts_dir: Path,
    agent_id: int,
):
    """Build a PreToolUse hook that timestamps + records each call.

    `claim_id_ref` is a mutable single-element dict so the worker can
    swap the active claim id between functions without rebuilding the
    hook closure.
    """
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcripts_dir / f"agent-{agent_id}.jsonl"
    state: dict[str, float] = {}

    async def hook(input_data: dict[str, Any], tool_use_id: str, context: Any) -> dict:
        tool_name = input_data.get("tool_name", "unknown")
        state[tool_use_id] = time.monotonic()
        queue.log_tool_event(
            worker_id=worker_id,
            claim_id=claim_id_ref.get("current"),
            tool_name=tool_name,
            phase="pre",
            input_payload={
                "tool_input_keys": sorted((input_data.get("tool_input") or {}).keys()),
            },
            output_excerpt=None,
            duration_ms=None,
        )
        _append_transcript(
            transcript_path,
            {
                "phase": "pre",
                "tool": tool_name,
                "claim_id": claim_id_ref.get("current"),
            },
        )
        return {}

    hook._tool_timings = state  # type: ignore[attr-defined]
    return hook


def make_posttool_hook(
    queue: WorkQueue,
    worker_id: int,
    claim_id_ref: dict[str, int | None],
    pretool_hook,
    transcripts_dir: Path,
    agent_id: int,
):
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcripts_dir / f"agent-{agent_id}.jsonl"

    async def hook(input_data: dict[str, Any], tool_use_id: str, context: Any) -> dict:
        tool_name = input_data.get("tool_name", "unknown")
        started = pretool_hook._tool_timings.pop(tool_use_id, None)  # type: ignore[attr-defined]
        duration_ms = int((time.monotonic() - started) * 1000) if started else None
        result = input_data.get("tool_response") or input_data.get("result") or {}
        excerpt = _excerpt(result)
        queue.log_tool_event(
            worker_id=worker_id,
            claim_id=claim_id_ref.get("current"),
            tool_name=tool_name,
            phase="post",
            input_payload=None,
            output_excerpt=excerpt,
            duration_ms=duration_ms,
        )
        _append_transcript(
            transcript_path,
            {
                "phase": "post",
                "tool": tool_name,
                "claim_id": claim_id_ref.get("current"),
                "duration_ms": duration_ms,
                "excerpt": excerpt[:200] if excerpt else None,
            },
        )
        return {}

    return hook


def _excerpt(result: Any, limit: int = 400) -> str | None:
    if result is None:
        return None
    if isinstance(result, str):
        return result[:limit]
    if isinstance(result, dict):
        for key in ("content", "stdout", "text", "output"):
            v = result.get(key)
            if isinstance(v, str):
                return v[:limit]
        return str(result)[:limit]
    return str(result)[:limit]


def _append_transcript(path: Path, payload: dict) -> None:
    import json

    with path.open("a") as f:
        f.write(json.dumps(payload, default=str) + "\n")
