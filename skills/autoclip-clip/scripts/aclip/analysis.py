"""The analysis contract — chunk plan, Candidate schema, chunk cache, write seam.

Deterministic layer around the agent-run analysis. ZERO model calls, ZERO network,
stdlib only (the gate suite asserts that structurally). The LLM-shaped work itself
stays subagent work (spec decisions 3/8): this module builds the contract AROUND
it — the chunk plan the clip skill (#33) builds its strictly-sequential work list
from, the Candidate schema every subagent answer is validated against, and the
per-Chunk cache that keeps a cached Chunk from ever being re-queried (AGENTS.md:
"a cached analysis chunk is never re-queried").

Module entry (NOT an ``aclip`` subcommand — the frozen CLI list is untouched,
per the ticket's hard frame #2):

    python -m aclip.analysis plan <project_dir> [--duration SEC]
    python -m aclip.analysis validate <candidates.json>
    python -m aclip.analysis record <project_dir> <chunk_no> <candidates.json>

Library seam (what the clip skill calls):

    plan = analysis.build_plan(project_dir)        # the dry-run JSON, as a dict
    analysis.plan_chunks(duration, settings)       # the pure chunk math
    analysis.validate_candidates(data)             # -> list of violations
    analysis.record(project_dir, chunk_no, data)   # validate + install the cache
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from aclip import settings as settings_module

# The chunk-length floor (the app-era 0.5-min minimum, src/analyze.rs) so the
# tiling loop stays finite even on a hostile settings value.
MIN_CHUNK_MINUTES = 0.5

# The Candidate schema: the app era's five RawRow fields (src/analyze.rs),
# tightened — score an int 0-100, title/reason non-empty strings, start/end
# finite seconds with 0 <= start < end. Every violation names the field.
CANDIDATE_FIELDS: tuple[str, ...] = ("score", "title", "reason", "start", "end")

# SRT cue line: "HH:MM:SS,mmm --> HH:MM:SS,mmm".
_SRT_RE = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d+)\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d+)"
)


# --- errors ---------------------------------------------------------------------


class CandidateError(ValueError):
    """A Candidates payload failed the schema; ``violations`` names each field."""

    def __init__(self, violations: Sequence[str]) -> None:
        self.violations = list(violations)
        super().__init__("; ".join(self.violations))


# --- the chunk plan ---------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    """One Chunk of the plan: 1-based index, inclusive video-absolute seconds."""

    index: int
    start: float
    end: float

    @property
    def name(self) -> str:
        """The plan's handle and the cache file's stem: ``chunk_001``."""
        return f"chunk_{self.index:03d}"


def chunk_seconds(settings: Any) -> float:
    """The chunk length in seconds (settings ``chunk_minutes``, floored)."""
    return max(float(settings.chunk_minutes), MIN_CHUNK_MINUTES) * 60.0


def plan_chunks(duration: float, settings: Any) -> list[Chunk]:
    """Tile ``[intro_sec, duration)`` into chunks of ``chunk_minutes`` minutes.

    The first ``intro_sec`` seconds are the cold open — never analysis material
    (spec decision 9), so no Chunk covers them. A trailing partial Chunk is kept
    (dropping it would silently shrink Coverage), and ``duration <= intro_sec``
    is an EMPTY plan, explicit: a cold-open-only video has no analysis.
    """
    duration = float(duration)
    intro = max(float(settings.intro_sec), 0.0)
    if duration <= intro:
        return []
    step = chunk_seconds(settings)
    chunks: list[Chunk] = []
    start = intro
    index = 1
    while start < duration:
        end = min(start + step, duration)
        chunks.append(Chunk(index, start, end))
        start = end
        index += 1
    return chunks


def fmt_hms(seconds: float) -> str:
    """``H:MM:SS`` display time (int(round(sec)), the app-era fmt_ts)."""
    total = int(round(max(0.0, float(seconds))))
    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _num(value: float) -> float | int:
    """Canonical number for JSON: int when integral, else the float."""
    as_float = float(value)
    return int(as_float) if as_float.is_integer() else as_float


# --- the Candidate schema -------------------------------------------------------


def _is_number(value: Any) -> bool:
    """A finite JSON number; bool is not a number here (True is not a score)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def validate_candidates(data: Any) -> list[str]:
    """Schema-check a Candidates payload; every violation names its field.

    ``data`` must be a list of candidate objects with the five schema fields:
    ``score`` an int 0-100; ``title`` and ``reason`` non-empty strings;
    ``start``/``end`` finite numbers of seconds with ``0 <= start < end``.
    Unknown extra fields are ignored. Returns [] when the payload is clean.
    """
    violations: list[str] = []
    if not isinstance(data, list):
        return ["candidates JSON must be a list of candidate objects"]
    for i, item in enumerate(data):
        where = f"items[{i}]"
        if not isinstance(item, Mapping):
            violations.append(f"{where}: must be an object, not {type(item).__name__}")
            continue
        # score — int 0-100 (the app coerced floats; the schema fixes int)
        if item.get("score") is None:
            violations.append(f"{where}: score is required (integer 0-100)")
        elif isinstance(item["score"], bool) or not isinstance(item["score"], int):
            violations.append(f"{where}: score must be an integer 0-100 (got {item['score']!r})")
        elif not 0 <= item["score"] <= 100:
            violations.append(f"{where}: score must be 0-100 (got {item['score']})")
        # title / reason — non-empty strings (a candidate without a reason is
        # not reviewable by a human in the end-of-run report)
        for field in ("title", "reason"):
            value = item.get(field)
            if value is None:
                violations.append(f"{where}: {field} is required (non-empty string)")
            elif not isinstance(value, str):
                violations.append(f"{where}: {field} must be a string (got {value!r})")
            elif not value.strip():
                violations.append(f"{where}: {field} must be a non-empty string")
        # start / end — finite seconds, 0 <= start < end
        for field in ("start", "end"):
            value = item.get(field)
            if value is None:
                violations.append(f"{where}: {field} is required (seconds, a number)")
            elif not _is_number(value):
                violations.append(f"{where}: {field} must be a number of seconds (got {value!r})")
        start, end = item.get("start"), item.get("end")
        if _is_number(start) and _is_number(end):
            if float(start) < 0 or float(end) < 0:
                violations.append(
                    f"{where}: start/end must not be negative (start={start}, end={end})"
                )
            if not float(start) < float(end):
                violations.append(f"{where}: start must be < end (start={start}, end={end})")
    return violations


# --- the per-Chunk cache --------------------------------------------------------


@dataclass(frozen=True)
class CacheState:
    """The verdict for one Chunk's cache file: HIT (replay) or MISS (reason)."""

    hit: bool
    reason: str
    items: list[Any] | None = None
    model: str | None = None


def analyze_dir(project_dir: Path | str) -> Path:
    """The project's ``analyze/`` dir (frozen tree; never a whole project)."""
    return Path(project_dir) / "analyze"


def chunk_cache_path(project_dir: Path | str, index: int) -> Path:
    """``projects/<Video Id>/analyze/chunk_NNN.json`` (1-based, 3 digits)."""
    return analyze_dir(project_dir) / f"chunk_{index:03d}.json"


def read_cache(project_dir: Path | str, index: int) -> CacheState:
    """Read one Chunk's cache file. HIT only when it is trustworthy: parsed
    JSON, a list at ``items``, a ``chunk`` field matching this index, and every
    item passing the Candidate schema. Bad JSON, wrong shape, wrong index, or
    schema-invalid items are all MISSes — an untrustworthy cache is never
    replayed (it is a repair target for ``record``, not evidence).
    """
    path = chunk_cache_path(project_dir, index)
    if not path.is_file():
        return CacheState(False, "not cached")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return CacheState(False, f"unreadable: {exc}")
    if not isinstance(data, dict):
        return CacheState(False, "cache file is not an object")
    if data.get("chunk") != index:
        return CacheState(False, f"chunk field {data.get('chunk')!r} != {index}")
    items = data.get("items")
    if not isinstance(items, list):
        return CacheState(False, "items is not a list")
    bad = validate_candidates(items)
    if bad:
        return CacheState(False, "schema-invalid items: " + "; ".join(bad[:3]))
    model = data.get("model")
    return CacheState(
        True,
        "cached",
        items,
        model if isinstance(model, str) and model else None,
    )


@dataclass(frozen=True)
class RecordResult:
    """What ``record`` did: ``cached`` (NO-OP) or ``recorded`` (installed)."""

    status: str
    path: Path
    note: str


def record(
    project_dir: Path | str,
    chunk_no: int,
    candidates: Any,
    settings: Any = None,
) -> RecordResult:
    """Validate a Candidates payload, then install it as the Chunk's cache file.

    Validates first (a schema violation names every bad field and writes
    nothing). A Chunk whose cache already holds a valid file is a NO-OP that
    reports the cache hit — the already-cached Chunk is never overwritten, and
    by contract never re-queried. An unreadable/corrupt file is a repair target:
    the payload replaces it, atomically (write a temp file in ``analyze/``, then
    ``os.replace``), so a crash never leaves a half-written cache. Only the
    ``analyze/`` subdir is created — never a whole project.
    """
    if int(chunk_no) < 1:
        raise CandidateError([f"chunk_no must be a positive integer (got {chunk_no!r})"])
    index = int(chunk_no)
    violations = validate_candidates(candidates)
    if violations:
        raise CandidateError(violations)
    path = chunk_cache_path(project_dir, index)
    state = read_cache(project_dir, index)
    if state.hit:
        return RecordResult(
            "cached",
            path,
            f"cache hit — chunk_{index:03d} is already cached and was not overwritten",
        )
    resolved = settings if settings is not None else settings_module.load_settings()
    payload = {"chunk": index, "model": resolved.model, "items": candidates}
    directory = analyze_dir(project_dir)
    directory.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=f".chunk_{index:03d}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            json.dump(payload, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name is not None and os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return RecordResult(
        "recorded",
        path,
        f"recorded {len(candidates)} candidate(s) → {path}",
    )


# --- duration sources -----------------------------------------------------------


def duration_from_words(path: Path | str) -> float:
    """The duration from a words transcript artifact.

    Shape: ``{"words": [{"start", "end", "word"}, ...]}``, also tolerating a
    ``segments`` wrapper (``[{"start", "end", "text"}]`` or segments carrying
    their own ``words``) and a bare list. The duration is the last timed end.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries: list[Any] = []
    if isinstance(data, dict):
        raw = data.get("words")
        if isinstance(raw, list):
            entries = raw
        elif isinstance(data.get("segments"), list):
            for segment in data["segments"]:
                if isinstance(segment, Mapping):
                    words = segment.get("words")
                    if isinstance(words, list):
                        entries.extend(words)
                    elif _is_number(segment.get("end")):
                        entries.append(segment)
    elif isinstance(data, list):
        entries = data
    ends = [
        float(entry["end"])
        for entry in entries
        if isinstance(entry, Mapping) and _is_number(entry.get("end"))
    ]
    if not ends:
        raise ValueError(f"no timed words in {path}")
    return max(ends)


def duration_from_srt(path: Path | str) -> float:
    """The duration from an SRT transcript (the last cue's end)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    ends: list[float] = []
    for match in _SRT_RE.finditer(text):
        h2, m2, s2, ms2 = (int(g) for g in match.group(5, 6, 7, 8))
        ends.append(h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0)
    if not ends:
        raise ValueError(f"no cues in {path}")
    return max(ends)


def duration_from_transcript(project_dir: Path | str) -> float:
    """Derive the media duration from the project's transcript artifact.

    ``transcript/*.words.json`` preferred, then ``transcript/*.srt`` (the
    transcript-fetch #25 output shapes). Missing both is an error naming the
    directory and the patterns looked for; the CLI's ``--duration SEC`` flag
    bypasses this entirely (and wins over any artifact).
    """
    directory = Path(project_dir) / "transcript"
    for path in sorted(directory.glob("*.words.json")):
        return duration_from_words(path)
    for path in sorted(directory.glob("*.srt")):
        return duration_from_srt(path)
    raise FileNotFoundError(
        f"no transcript artifact in {directory} "
        "(looked for *.words.json and *.srt); pass --duration SEC instead"
    )


# --- the dry-run plan -----------------------------------------------------------


def build_plan(
    project_dir: Path | str,
    duration: float | None = None,
    settings: Any = None,
) -> dict[str, Any]:
    """The full dry-run plan as a JSON-able dict, zero model calls.

    Duration comes from ``--duration``/``duration`` when given, else the
    project's transcript artifact. Each Chunk reports its boundaries plus a
    cache ``hit``/``miss``; HIT Chunks never appear in ``to_query`` — that list
    is the clip skill's work list, so a cached Chunk is never re-queried. The
    plan carries ``parallelism``/``sequential`` from settings (#33 enforces the
    one-subagent-at-a-time rule at run time) and the model resolution: the
    settings ``model:`` override, else a note that the Coding Agent's own model
    answers. Settings are re-read per call unless one is passed in.
    """
    resolved = settings if settings is not None else settings_module.load_settings()
    duration_source = "--duration" if duration is not None else "transcript artifact"
    if duration is None:
        duration = duration_from_transcript(project_dir)
    duration_seconds = float(duration)
    chunks = plan_chunks(duration_seconds, resolved)
    entries: list[dict[str, Any]] = []
    hits = 0
    for chunk in chunks:
        state = read_cache(project_dir, chunk.index)
        entries.append(
            {
                "index": chunk.index,
                "name": chunk.name,
                "start": _num(chunk.start),
                "end": _num(chunk.end),
                "duration_sec": _num(round(chunk.end - chunk.start, 3)),
                "start_ts": fmt_hms(chunk.start),
                "end_ts": fmt_hms(chunk.end),
                "cache": "hit" if state.hit else "miss",
                "cache_note": state.reason,
            }
        )
        if state.hit:
            hits += 1
    model = resolved.model
    return {
        "project_dir": str(project_dir),
        "settings_path": str(resolved.path) if resolved.path is not None else None,
        "duration_source": duration_source,
        "duration_sec": _num(duration_seconds),
        "intro_sec": _num(resolved.intro_sec),
        "chunk_minutes": _num(resolved.chunk_minutes),
        "chunks": entries,
        "chunk_count": len(entries),
        "cache_hits": hits,
        "cache_misses": len(entries) - hits,
        "to_query": [entry["name"] for entry in entries if entry["cache"] == "miss"],
        "parallelism": resolved.parallelism,
        "sequential": resolved.parallelism == 1,
        "model": model,
        "model_note": model if model else "the Coding Agent's own model",
    }


# --- the module entry (python -m aclip.analysis) --------------------------------


def _load_candidates_json(path: Path | str) -> Any:
    """Read a Candidates JSON file; clear errors naming the file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc


def _print_violations(prefix: str, violations: Sequence[str]) -> None:
    print(f"{prefix} ({len(violations)} violation(s)):", file=sys.stderr)
    for violation in violations:
        print(f"  - {violation}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aclip.analysis",
        description=(
            "The analysis contract: chunk plan, Candidate schema, chunk cache. "
            "Deterministic — zero model calls, zero network."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan_parser = sub.add_parser(
        "plan", help="print the full dry-run plan as JSON (boundaries, cache hit/miss, parallelism, model)"
    )
    plan_parser.add_argument("project_dir", help="projects/<Video Id>")
    plan_parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="media duration in seconds; wins over the project's transcript artifact",
    )

    validate_parser = sub.add_parser(
        "validate", help="schema-check a Candidates JSON file (exit 0 silent when clean)"
    )
    validate_parser.add_argument("candidates_json", help="the Candidates JSON file to check")

    record_parser = sub.add_parser(
        "record", help="validate a Candidates JSON file, then install it as analyze/chunk_NNN.json"
    )
    record_parser.add_argument("project_dir", help="projects/<Video Id>")
    record_parser.add_argument("chunk_no", type=int, help="the 1-based Chunk number")
    record_parser.add_argument("candidates_json", help="the Candidates JSON file to record")

    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = build_plan(args.project_dir, duration=args.duration)
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        if args.command == "validate":
            data = _load_candidates_json(args.candidates_json)
            violations = validate_candidates(data)
            if violations:
                _print_violations(f"{args.candidates_json}: candidates rejected", violations)
                return 1
            return 0
        if args.command == "record":
            data = _load_candidates_json(args.candidates_json)
            try:
                result = record(args.project_dir, args.chunk_no, data)
            except CandidateError as exc:
                _print_violations(f"record chunk_{args.chunk_no:03d} rejected", exc.violations)
                return 1
            if result.status == "cached":
                print(result.note, file=sys.stderr)
                return 0
            print(result.note)
            return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":  # python -m aclip.analysis …
    sys.exit(main())
