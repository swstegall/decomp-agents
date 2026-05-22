"""Prompt loading. Kept in prompts/*.md so they can be edited without touching code."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


@lru_cache(maxsize=4)
def load_worker_system_prompt() -> str:
    return (PROMPT_DIR / "worker_system.md").read_text()


@lru_cache(maxsize=4)
def load_merge_resolver_system_prompt() -> str:
    return (PROMPT_DIR / "merge_resolver_system.md").read_text()
