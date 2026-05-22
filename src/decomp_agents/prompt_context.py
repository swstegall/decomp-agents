"""Pre-load per-function metadata + sibling examples for the worker brief.

Why this exists:
  Without help, an agent burns ~3-5 turns rediscovering facts that are
  already in `config/<binary>.{yaml,symbols.json,rtti.json}` and figuring
  out which sibling matches to read. This module folds those discoveries
  into the brief up-front.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .work_queue import Function


@dataclass
class PromptContext:
    asm_relpath: str
    pseudo_c_relpath: str | None
    rtti_class: str | None
    rtti_vtable_rva_hex: str | None
    sibling_paths: list[str] = field(default_factory=list)
    idioms_path: str | None = None
    blocked_post_mortem_path: str | None = None
    blocked_post_mortem_excerpt: str | None = None
    nearby_types_paths: list[str] = field(default_factory=list)
    nearby_matched_count: int = 0

    def to_markdown(self, *, max_iters: int, fn: Function) -> str:
        lines: list[str] = []
        lines.append("## Inputs you can read RIGHT NOW")
        lines.append("")
        lines.append(f"- Disassembly: `{self.asm_relpath}`")
        if self.pseudo_c_relpath:
            lines.append(
                f"- Headless Ghidra pseudo-C: `{self.pseudo_c_relpath}` "
                f"(treat as a strong hint, not gospel — byte-correctness "
                f"comes from the asm)"
            )
        else:
            lines.append(
                "- Headless Ghidra pseudo-C: NOT AVAILABLE for this binary "
                "(no `build/ghidra-decomp/<binary>/` dump yet). Work from "
                "the asm + sibling examples; do not try to open `*.gpr` — "
                "that's a GUI artefact you cannot read."
            )
        if self.rtti_class:
            lines.append(
                f"- RTTI: this function's RVA is inside class `{self.rtti_class}` "
                f"(vtable at `{self.rtti_vtable_rva_hex}`). Treat it as a "
                f"member function with `__thiscall` calling convention unless "
                f"the asm prologue clearly says otherwise."
            )
        lines.append(f"- Function YAML row: `config/{fn.binary}.yaml`")
        lines.append(f"- Symbol metadata: `config/{fn.binary}.symbols.json`")
        lines.append(f"- RTTI catalog: `config/{fn.binary}.rtti.json`")

        if self.idioms_path:
            lines.append(
                f"- Binary-specific idioms (READ THIS FIRST): `{self.idioms_path}`"
            )

        if self.blocked_post_mortem_path:
            lines.append("")
            lines.append(
                f"## ⚠️  Prior attempt(s) on THIS function bailed — read first"
            )
            lines.append(
                f"`{self.blocked_post_mortem_path}` contains post-mortems "
                f"from earlier workers. Don't re-try what already failed."
            )
            if self.blocked_post_mortem_excerpt:
                lines.append("")
                lines.append("### Excerpt from the most recent attempt")
                lines.append("```")
                lines.append(self.blocked_post_mortem_excerpt)
                lines.append("```")

        if self.sibling_paths:
            lines.append("")
            lines.append("## Read these sibling matches FIRST (before writing your .cpp)")
            lines.append("")
            lines.append(
                "Picked heuristically from already-matched functions in the "
                "same module / similar size band. They show the local idiom."
            )
            for p in self.sibling_paths:
                lines.append(f"- `{p}`")

        if self.nearby_types_paths:
            lines.append("")
            lines.append("## Nearby type discoveries (from earlier matches)")
            for p in self.nearby_types_paths:
                lines.append(f"- `{p}`")

        if self.nearby_matched_count:
            lines.append("")
            lines.append(
                f"FYI: this binary already has {self.nearby_matched_count} "
                f"matched function(s) in `src/{fn.binary}/_rosetta/`. "
                f"You can grep that directory for any symbol name you encounter."
            )

        lines.append("")
        lines.append("## On bail: write a post-mortem")
        lines.append("")
        lines.append(
            f"If you exhaust your iteration budget (max {max_iters}) without "
            f"reaching GREEN, append a post-mortem block to "
            f"`decomp-notes/blocked/{fn.binary}/{fn.rva_hex}_<symbol>.md` "
            f"BEFORE you emit `BLOCKED:`. Format documented in "
            f"`decomp-notes/README.md`. Include: iterations tried, the diff "
            f"snapshot, your best guess at the root cause, and a one-line "
            f"hint for the next worker."
        )

        lines.append("")
        lines.append("## On match: optional type discovery note")
        lines.append("")
        lines.append(
            f"If during the match you identified a class / struct / vtable / "
            f"enum that should be catalogued, write `decomp-notes/types/"
            f"{fn.binary}/{fn.rva_hex}.md` per the README's format. Skip "
            f"this if you didn't learn anything reusable."
        )

        return "\n".join(lines)


def build_context(
    fn: Function,
    *,
    repo: Path,
    max_siblings: int = 3,
) -> PromptContext:
    asm_relpath = (
        Path("asm")
        / fn.binary
        / f"{fn.rva:08x}_{_safe_symbol(fn.symbol)}.s"
    )
    pseudo_c = (
        Path("build")
        / "ghidra-decomp"
        / fn.binary
        / f"{fn.rva:08x}_{_safe_symbol(fn.symbol)}.c"
    )
    has_pseudo_c = (repo / pseudo_c).is_file()

    rtti_class, rtti_vtable = _resolve_rtti(repo, fn)
    idioms_path = _idioms_path(repo, fn)
    blocked_path, blocked_excerpt = _blocked_history(repo, fn)
    siblings = _pick_siblings(repo, fn, max_siblings=max_siblings)
    nearby_types = _nearby_types(repo, fn)
    nearby_matched = _nearby_matched_count(repo, fn)

    return PromptContext(
        asm_relpath=str(asm_relpath),
        pseudo_c_relpath=str(pseudo_c) if has_pseudo_c else None,
        rtti_class=rtti_class,
        rtti_vtable_rva_hex=rtti_vtable,
        sibling_paths=[str(p) for p in siblings],
        idioms_path=str(idioms_path) if idioms_path else None,
        blocked_post_mortem_path=str(blocked_path) if blocked_path else None,
        blocked_post_mortem_excerpt=blocked_excerpt,
        nearby_types_paths=[str(p) for p in nearby_types],
        nearby_matched_count=nearby_matched,
    )


# --- helpers ---------------------------------------------------------------


def _safe_symbol(sym: str) -> str:
    return sym.replace("::", "__").replace("/", "_").replace(".", "_")


def _idioms_path(repo: Path, fn: Function) -> Path | None:
    p = Path("decomp-notes") / "idioms" / f"{fn.binary}.md"
    return p if (repo / p).is_file() else None


def _blocked_history(repo: Path, fn: Function) -> tuple[Path | None, str | None]:
    p = (
        Path("decomp-notes")
        / "blocked"
        / fn.binary
        / f"{fn.rva_hex}_{_safe_symbol(fn.symbol)}.md"
    )
    full = repo / p
    if not full.is_file():
        return None, None
    # Pull the most recent ## block as the excerpt.
    text = full.read_text(errors="replace")
    blocks = text.split("\n## ")
    if len(blocks) >= 2:
        excerpt = "## " + blocks[-1].strip()
    else:
        excerpt = text.strip()
    # Cap length so the prompt doesn't balloon.
    if len(excerpt) > 1200:
        excerpt = excerpt[:1200] + "\n…(truncated)"
    return p, excerpt


def _nearby_types(repo: Path, fn: Function) -> list[Path]:
    types_dir = repo / "decomp-notes" / "types" / fn.binary
    if not types_dir.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(types_dir.iterdir()):
        if not child.is_file() or not child.name.endswith(".md"):
            continue
        try:
            rva = int(child.stem, 16)
        except ValueError:
            continue
        # "Nearby" = within ±0x10000 of the function's RVA. The threshold
        # is intentionally loose — agents can ignore irrelevant entries.
        if abs(rva - fn.rva) < 0x10000:
            out.append(child.relative_to(repo))
    return out


def _resolve_rtti(repo: Path, fn: Function) -> tuple[str | None, str | None]:
    path = repo / "config" / f"{fn.binary}.rtti.json"
    if not path.is_file():
        return None, None
    try:
        rows = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None, None
    if not isinstance(rows, list):
        return None, None
    # The RTTI dump catalogues vtables, NOT functions. We check whether
    # the function's RVA matches any vtable's slot entries — but for the
    # initial pass we only ship the class name if the function's RVA is
    # within a known class's vftable block. A future enhancement could
    # resolve vtable[N] → function-RVA for member-fn identification.
    best: tuple[str, str] | None = None
    for row in rows:
        try:
            rva = int(row["rva_hex"], 16) if "rva_hex" in row else int(row["rva"])
        except (KeyError, ValueError, TypeError):
            continue
        # Heuristic: if a vtable lives within 0x100 of the function's RVA
        # in the same section, the function is plausibly a member of that
        # class. Imperfect but cheap. Agents are told to verify against
        # the asm prologue.
        if abs(rva - fn.rva) < 0x100:
            cls = row.get("class") or row.get("name") or "?"
            best = (cls, f"0x{rva:08x}")
    if best:
        return best
    return None, None


def _pick_siblings(repo: Path, fn: Function, *, max_siblings: int) -> list[Path]:
    """Heuristic: pick already-matched siblings most likely to share idioms.

    Priority:
      1. Same module, similar size (within 2x) — best signal.
      2. Same module, any size.
      3. Same binary, any module, similar size.

    Returns repo-relative paths. Returns at most max_siblings entries.
    """
    rosetta_dir = repo / "src" / fn.binary / "_rosetta"
    if not rosetta_dir.is_dir():
        return []

    yaml_path = repo / "config" / f"{fn.binary}.yaml"
    if not yaml_path.is_file():
        return []

    import yaml as _yaml  # local to avoid import cost when context isn't built

    try:
        rows = _yaml.safe_load(yaml_path.read_text()) or []
    except _yaml.YAMLError:
        return []

    candidates: list[tuple[int, int, str, str]] = []  # (priority, size, symbol, module)
    target_size = fn.size or 1
    for row in rows:
        if row.get("status") not in ("matched",):
            continue
        if row.get("type") != "matching":
            continue
        sym = row.get("symbol") or ""
        mod = row.get("module") or "_unknown"
        size = _coerce_int(row.get("size"))
        if size is None or sym == fn.symbol:
            continue
        # Skip auto-passthrough wrappers (the FUN_<addr> ones) when we
        # have hand-named alternatives — they're naked-asm and don't show
        # idioms.
        is_named = not sym.startswith("FUN_")
        same_module = mod == fn.module
        similar_size = 0.5 <= (size / target_size) <= 2.0 if target_size else True
        priority = (
            0 if (same_module and similar_size and is_named) else
            1 if (same_module and is_named) else
            2 if (same_module and similar_size) else
            3 if same_module else
            4 if (similar_size and is_named) else
            5
        )
        candidates.append((priority, size, sym, mod))

    # Sort by priority, then by size-distance to the target.
    candidates.sort(key=lambda c: (c[0], abs(c[1] - target_size)))

    out: list[Path] = []
    seen: set[str] = set()
    for _, _, sym, _ in candidates:
        if sym in seen:
            continue
        seen.add(sym)
        cpp_path = rosetta_dir / f"{_safe_symbol(sym)}.cpp"
        if cpp_path.is_file():
            out.append(cpp_path.relative_to(repo))
            if len(out) >= max_siblings:
                break
    return out


def _nearby_matched_count(repo: Path, fn: Function) -> int:
    rosetta_dir = repo / "src" / fn.binary / "_rosetta"
    if not rosetta_dir.is_dir():
        return 0
    try:
        return sum(1 for p in rosetta_dir.iterdir() if p.suffix == ".cpp")
    except OSError:
        return 0


def _coerce_int(v) -> int | None:
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v, 16) if v.lower().startswith("0x") else int(v)
        except ValueError:
            return None
    return None
