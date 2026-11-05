"""Deterministic curation — the unsupervised run's selection rules (ticket #31).

Spec decision 9: drop candidates outside ``clip_min_sec``/``clip_max_sec``
(30/90), skip the first ``intro_sec`` (90), merge overlapping candidates
highest-score-first (absorbed, never double-counted), take the top
``target_count`` (10) by score with a start-time tie-break, and apply
``min_score`` only when the user set a floor (``null`` = no floor). No taste,
no model call, no randomness: the same candidates + settings always produce the
same ordered selection.

This module ports the app era's ``merge_candidates`` (src/analyze.rs) with the
v1 changes the spec froze: NO IoU-threshold dedup (any overlap merges), NO
cue-snapping (render owns caption units now), NO Zones/Guard (out of v1 —
``intro_sec`` covers cold opens), bounds from settings instead of hardcoded 30.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from aclip import settings as settings_module

# The frozen defaults (settings.md) this module derives its rules from.
DEFAULT_CLIP_MIN_SEC = 30
DEFAULT_CLIP_MAX_SEC = 90
DEFAULT_INTRO_SEC = 90
DEFAULT_TARGET_COUNT = 10
DEFAULT_MIN_SCORE: float | None = None
#: The chunk-number scan ceiling: 999 six-minute chunks is 100 hours of audio,
#: far past any single video, and keeps a corrupt cache from looping forever.
MAX_CHUNKS = 999

#: Why a Candidate is or is not in the selection. The inventory artifact
#: (``analyze/candidates.json``) carries one of these against every candidate,
#: so "the full analysis" is a durable file and not a promise in a report.
DISPOSITION_SELECTED = "selected"
DISPOSITION_AVAILABLE = "available"  # ranked well, past the target_count cut
DISPOSITION_MERGED = "merged"  # overlapped a higher-scored keeper
DISPOSITION_DROPPED_BOUNDS = "dropped_out_of_bounds"
DISPOSITION_DROPPED_INTRO = "dropped_intro"
DISPOSITION_DROPPED_FLOOR = "dropped_below_floor"


@dataclass(frozen=True)
class CurationSettings:
    """The settings slice curation needs, with the frozen defaults."""

    clip_min_sec: float = DEFAULT_CLIP_MIN_SEC
    clip_max_sec: float = DEFAULT_CLIP_MAX_SEC
    intro_sec: float = DEFAULT_INTRO_SEC
    target_count: int = DEFAULT_TARGET_COUNT
    min_score: float | None = DEFAULT_MIN_SCORE

    @classmethod
    def from_settings(cls, loaded: Any) -> "CurationSettings":
        """Project the parsed settings.md onto curation's rule slice."""
        return cls(
            clip_min_sec=float(loaded.clip_min_sec),
            clip_max_sec=float(loaded.clip_max_sec),
            intro_sec=float(loaded.intro_sec),
            target_count=int(loaded.target_count),
            min_score=None if loaded.min_score is None else float(loaded.min_score),
        )


def _order_key(candidate: Mapping[str, Any]) -> tuple[float, float]:
    """Best-first ordering: score descending, then start ascending (stable)."""
    return (-float(candidate["score"]), float(candidate["start"]))


def _overlaps(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """True when two spans share any time. Touching endpoints do not overlap
    (a 30-45 s clip and a 45-60 s clip are neighbours, not duplicates)."""
    return float(a["start"]) < float(b["end"]) and float(b["start"]) < float(a["end"])


def _drop_reason(candidate: Mapping[str, Any], rules: CurationSettings) -> str | None:
    """Why a candidate never reaches the ranking: bounds, intro, or floor.

    ``None`` means it survives the drop phase (it may still be merged or land
    past the top-N cut). Bounds beat intro beat floor — the first reason a
    candidate fails is the one the inventory reports.
    """
    duration = float(candidate["end"]) - float(candidate["start"])
    if duration < rules.clip_min_sec or duration > rules.clip_max_sec:
        return DISPOSITION_DROPPED_BOUNDS
    if float(candidate["start"]) < rules.intro_sec:
        return DISPOSITION_DROPPED_INTRO
    if rules.min_score is not None and float(candidate["score"]) < rules.min_score:
        return DISPOSITION_DROPPED_FLOOR
    return None


def rank_candidates(
    candidates: Sequence[Mapping[str, Any]], rules: CurationSettings
) -> list[dict[str, Any]]:
    """Every candidate, best-first, with the disposition that explains it.

    The deterministic ranking behind the top-N cut. The merge phase walks the
    survivors best-first; a candidate overlapping an already-kept one is merged
    into it (never double-counted). Each returned row is a dict copy carrying
    ``rank`` (its 1-based position in the merged order, or ``None`` when it was
    dropped or merged) and ``disposition``. Pure: the input is not mutated.
    """
    survivors: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for candidate in candidates:
        row = dict(candidate)
        row["rank"] = None
        reason = _drop_reason(row, rules)
        if reason is None:
            survivors.append(row)
        else:
            row["disposition"] = reason
            row["merged_into"] = None
            dropped.append(row)

    kept: list[dict[str, Any]] = []
    merged: list[dict[str, Any]] = []
    for row in sorted(survivors, key=_order_key):
        keeper = next((k for k in kept if _overlaps(row, k)), None)
        if keeper is not None:
            row["disposition"] = DISPOSITION_MERGED
            row["merged_into"] = {
                "start": keeper.get("start"),
                "title": keeper.get("title") or keeper.get("generated_title") or "",
            }
            merged.append(row)
            continue
        row["disposition"] = DISPOSITION_SELECTED  # the cut relabels it below
        row["merged_into"] = None
        row["rank"] = len(kept) + 1
        kept.append(row)

    # best-first across the whole inventory, dropped or not: the report and the
    # inventory artifact read in score order, so the cut line is visible at a
    # glance
    return sorted([*dropped, *merged, *kept], key=_order_key)


def select(
    candidates: Sequence[Mapping[str, Any]],
    rules: CurationSettings | None = None,
    skip: int = 0,
) -> list[Mapping[str, Any]]:
    """Select a slice of the ranking from scored candidates, deterministically.

    Rules, in order (spec decision 9):
      1. bounds — drop spans shorter than ``clip_min_sec`` or longer than
         ``clip_max_sec`` (a span exactly at a bound is kept);
      2. intro — drop candidates that start inside the first ``intro_sec``
         seconds (the cold open is not clip material);
      3. floor — drop candidates below ``min_score`` when a floor is set
         (``None`` = no floor, the frozen default);
      4. merge — walking the survivors best-first, any candidate overlapping an
         already-kept one is absorbed (dropped, never double-counted); the
         higher-scored keeper's span is unchanged;
      5. window — keep the ``target_count`` merged rows at offset ``skip``.
         ``skip=0`` (the default) is the top of the ranking; ``skip=10`` is the
         11th..20th, which is what a continuation batch renders.
      6. order — the selection is returned best-first (score desc, tie-break by
         start time), the order the end-of-run report lists.
    Pure: the input list is not mutated.
    """
    if rules is None:
        rules = CurationSettings()

    survivors = []
    for candidate in candidates:
        if _drop_reason(candidate, rules) is not None:
            continue
        survivors.append(candidate)

    ordered = sorted(survivors, key=_order_key)
    kept: list[Mapping[str, Any]] = []
    for candidate in ordered:
        if any(_overlaps(candidate, keeper) for keeper in kept):
            continue  # absorbed into the higher-scored keeper, never counted twice
        kept.append(candidate)

    count = max(rules.target_count, 0)
    start = max(int(skip), 0)
    return list(kept[start : start + count])


def select_from_settings(
    candidates: Sequence[Mapping[str, Any]], loaded: Any = None
) -> list[Mapping[str, Any]]:
    """``select`` with the rules projected from the parsed settings.md."""
    if loaded is None:
        loaded = settings_module.load_settings()
    return select(candidates, CurationSettings.from_settings(loaded))


def build_selection(
    project_dir: Path | str, settings: Any = None, skip: int = 0
) -> dict[str, Any]:
    """Cull the cached Candidates into the deterministic selection (spec
    decision 9) and return the selection payload the render renders:
    ``{"video_id", "clips": [...]}`` — score-ordered, ids and ranks assigned
    from the ranking's offset ``skip`` (so a continuation batch keeps the
    selection's own ranks 11, 12, … and never renames an already-rendered
    clip). Reads the analysis cache only — a MISS chunk has no candidates."""
    from aclip import analysis

    project_dir = Path(project_dir)
    resolved = settings if settings is not None else settings_module.load_settings()
    rules = CurationSettings.from_settings(resolved)
    candidates, inventory = _read_candidates(project_dir, rules, skip)
    selected = select(candidates, rules, skip)
    clips = []
    for offset, candidate in enumerate(selected, start=max(int(skip), 0) + 1):
        clips.append(
            {
                "id": f"clip_{offset:03d}",
                "rank": offset,
                "generated_title": candidate.get("title") or candidate.get("reason") or f"clip {offset}",
                "score": candidate["score"],
                "reason": candidate.get("reason", ""),
                "start": candidate["start"],
                "end": candidate["end"],
            }
        )
    payload = {
        "video_id": project_dir.name,
        "settings_path": str(getattr(resolved, "path", "") or ""),
        "candidate_count": len(candidates),
        "selection_count": len(clips),
        "skip": max(int(skip), 0),
        "clips": clips,
    }
    payload["inventory"] = inventory
    return payload


def _read_candidates(
    project_dir: Path, rules: CurationSettings, skip: int = 0
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The cached Candidates plus the ranked inventory of every one of them.

    The selection is only ever the top ``target_count`` — but the run's whole
    finding is what gets cached, so the inventory carries every candidate with
    its disposition: selected, available (ranked well, past the cut — what a
    continuation batch picks up), merged into a higher-scored keeper, or
    dropped for bounds / intro / floor. It is the "full analysis" artifact the
    run report points at, and it is durable: a later continuation needs no
    model call to know what is left.
    """
    from aclip import analysis

    candidates: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    for number in range(1, MAX_CHUNKS + 1):
        state = analysis.read_cache(project_dir, number)
        if not state.hit:
            if number > 1:
                break  # chunks are contiguous: past the last cached one
            continue
        chunk_candidates: list[dict[str, Any]] = []
        for item in state.items or []:
            entry = dict(item)
            entry.setdefault("title", entry.get("generated_title", ""))
            candidates.append(entry)
            chunk_candidates.append(entry)
        chunks.append({"chunk": number, "candidate_count": len(chunk_candidates)})
    ranked = rank_candidates(candidates, rules)
    # the merge phase marks every survivor SELECTED; the top-N cut (offset by
    # ``skip``) splits them into what this batch renders and what a
    # continuation batch picks up
    window = range(max(int(skip), 0) + 1, max(int(skip), 0) + max(rules.target_count, 0) + 1)
    for row in ranked:
        rank = row["rank"]
        if rank is None:
            continue  # dropped or merged — its disposition is already final
        row["disposition"] = (
            DISPOSITION_SELECTED if rank in window else DISPOSITION_AVAILABLE
        )
    inventory = {
        "video_id": project_dir.name,
        "candidate_count": len(candidates),
        "chunks": chunks,
        "candidates": ranked,
    }
    return candidates, inventory


def write_selection(project_dir: Path | str, payload: dict[str, Any]) -> Path:
    from aclip import analysis

    path = analysis.analyze_dir(project_dir) / "selection.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {key: value for key, value in payload.items() if key != "inventory"}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_inventory(project_dir: Path | str, payload: dict[str, Any]) -> Path:
    """Write the ranked inventory of every cached Candidate to
    ``analyze/candidates.json`` (the full analysis, with dispositions)."""
    from aclip import analysis

    path = analysis.analyze_dir(project_dir) / "candidates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload.get("inventory", payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m aclip.curate <project_dir> [--skip N] [--count N]``.

    Writes ``analyze/selection.json`` (the clips the render renders) and
    ``analyze/candidates.json`` (every cached Candidate with its disposition).
    ``--skip N`` starts the selection at rank N+1 — the continuation form:
    after ten clips have rendered, ``--skip 10`` selects the 11th..20th.
    """
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    named = ["--skip", "--count"]
    positional: list[str] = []
    values: dict[str, int] = {}
    index = 0
    while index < len(argv):
        arg = argv[index]
        name = next((key for key in named if arg == key or arg.startswith(key + "=")), None)
        if name is not None:
            if arg.startswith(name + "="):
                values[name] = int(arg[len(name) + 1 :])
            elif index + 1 < len(argv):
                values[name] = int(argv[index + 1])
                index += 1  # the value is consumed, not a positional
            index += 1
            continue
        positional.append(arg)
        index += 1
    if not positional or any(arg in {"-h", "--help"} for arg in argv):
        print(
            "usage: python -m aclip.curate <project_dir> [--skip N] [--count N]",
            file=sys.stderr,
        )
        return 0 if any(arg in {"-h", "--help"} for arg in argv) else 2
    project_dir = Path(positional[0])
    skip = int(values.get("--skip", 0))
    count = values.get("--count")
    payload = build_selection(project_dir, skip=skip)
    if count is not None:
        payload["clips"] = payload["clips"][: max(int(count), 0)]
        payload["selection_count"] = len(payload["clips"])
    write_selection(project_dir, payload)
    inventory_path = write_inventory(project_dir, payload)
    print(str(selection_path(project_dir)))
    print(str(inventory_path))
    available = sum(
        1 for row in payload["inventory"]["candidates"] if row["disposition"] == DISPOSITION_AVAILABLE
    )
    print(
        f"selection: {payload['selection_count']} clip(s) from "
        f"{payload['candidate_count']} candidate(s) "
        f"(skip={payload['skip']}, {available} available beyond the cut)"
    )
    return 0


def selection_path(project_dir: Path | str) -> Path:
    """``analyze/selection.json`` — the payload the render renders."""
    return Path(project_dir) / "analyze" / "selection.json"


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    import sys

    sys.exit(main())
