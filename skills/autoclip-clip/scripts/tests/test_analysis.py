"""The analysis contract: chunk plan, Candidate schema, cache, dry-run (ticket #30).

Everything here is deterministic — zero model calls, zero network. The module
seam is exercised through ``plan_chunks``/``validate_candidates``/``read_cache``/
``record``/``build_plan`` and the ``python -m aclip.analysis`` entry, which is
what the clip skill (#33) actually calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aclip import analysis, settings

GOOD_CANDIDATES = [
    {"score": 88, "title": "The 410 story", "reason": "a concrete mid-run failure", "start": 95, "end": 160},
    {"score": 61, "title": "Rate limits", "reason": "the 40 RPM rule explained", "start": 810, "end": 1000},
]


def _loaded(analysis_settings):
    """Load the tmp settings.md the fixture wired up."""
    return settings.load_settings()


# --- chunk plan ------------------------------------------------------------------


def test_plan_tiles_after_the_intro_with_a_trailing_partial(analysis_settings) -> None:
    loaded = _loaded(analysis_settings)  # intro 90, chunk 6 min = 360 s
    chunks = analysis.plan_chunks(1000.0, loaded)
    assert [(c.index, c.start, c.end) for c in chunks] == [
        (1, 90, 450),
        (2, 450, 810),
        (3, 810, 1000),  # the trailing partial chunk is kept, not dropped
    ]
    assert [c.name for c in chunks] == ["chunk_001", "chunk_002", "chunk_003"]


def test_plan_skips_the_cold_open(analysis_settings) -> None:
    loaded = _loaded(analysis_settings(intro_sec="45"))
    chunks = analysis.plan_chunks(300.0, loaded)
    assert chunks and all(c.start >= 45 for c in chunks)
    assert chunks[0].start == 45


def test_plan_is_empty_when_duration_fits_inside_the_intro(analysis_settings) -> None:
    loaded = _loaded(analysis_settings)
    assert analysis.plan_chunks(90.0, loaded) == []
    assert analysis.plan_chunks(30.0, loaded) == []


def test_plan_boundaries_touch_without_overlap(analysis_settings) -> None:
    loaded = _loaded(analysis_settings(chunk_minutes="1"))
    chunks = analysis.plan_chunks(400.0, loaded)
    assert len(chunks) == 6  # 360 s after the 90 s intro, 60 s each
    for previous, current in zip(chunks, chunks[1:]):
        assert previous.end == current.start


# --- the Candidate schema --------------------------------------------------------


def test_valid_candidates_pass() -> None:
    assert analysis.validate_candidates(GOOD_CANDIDATES) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"score": 101, "title": "t", "reason": "r", "start": 0, "end": 1},  # score > 100
        {"score": -1, "title": "t", "reason": "r", "start": 0, "end": 1},  # score < 0
        {"score": True, "title": "t", "reason": "r", "start": 0, "end": 1},  # bool is not a score
        {"score": 88, "title": "t", "reason": "r", "start": 5, "end": 5},  # start == end
        {"score": 88, "title": "t", "reason": "r", "start": 9, "end": 4},  # start > end
        {"score": 88, "title": "t", "reason": "r", "start": -3, "end": 4},  # negative
        {"score": 88, "title": "", "reason": "r", "start": 0, "end": 1},  # empty title
        {"score": 88, "title": "t", "reason": "r"},  # missing start/end
        {"score": 88, "title": "t", "reason": "r", "start": 0, "end": "x"},  # non-numeric
    ],
)
def test_schema_rejects_naming_the_field(bad: dict) -> None:
    violations = analysis.validate_candidates([bad])
    assert violations, "expected a violation"
    assert any("items[0]" in v for v in violations)


def test_schema_rejects_a_non_list_payload() -> None:
    assert analysis.validate_candidates({"score": 1}) == ["candidates JSON must be a list of candidate objects"]


# --- the per-Chunk cache ---------------------------------------------------------


def _seed_cache(project: Path, index: int, payload: dict | str) -> Path:
    path = analysis.chunk_cache_path(project, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def test_a_valid_cache_file_is_a_hit(analysis_project: Path) -> None:
    _seed_cache(analysis_project, 1, {"chunk": 1, "model": None, "items": GOOD_CANDIDATES})
    state = analysis.read_cache(analysis_project, 1)
    assert state.hit and state.items == GOOD_CANDIDATES


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",  # unreadable
        json.dumps([1, 2, 3]),  # not an object
        json.dumps({"chunk": 2, "items": []}),  # wrong chunk index
        json.dumps({"chunk": 1, "items": {"a": 1}}),  # items not a list
        json.dumps({"chunk": 1, "items": [{"score": 999, "title": "x", "reason": "y", "start": 0, "end": 1}]}),
    ],
)
def test_untrustworthy_cache_files_are_misses(analysis_project: Path, payload: str) -> None:
    _seed_cache(analysis_project, 1, payload)
    state = analysis.read_cache(analysis_project, 1)
    assert not state.hit
    assert state.reason


def test_no_cache_file_is_a_miss(analysis_project: Path) -> None:
    state = analysis.read_cache(analysis_project, 3)
    assert not state.hit and state.reason == "not cached"


# --- record: validate on write, never overwrite a cached chunk -------------------


def test_record_installs_the_cache_file(analysis_project: Path, analysis_settings) -> None:
    result = analysis.record(analysis_project, 1, GOOD_CANDIDATES)
    assert result.status == "recorded"
    assert result.path == analysis.chunk_cache_path(analysis_project, 1)
    stored = json.loads(result.path.read_text(encoding="utf-8"))
    assert stored["chunk"] == 1 and stored["items"] == GOOD_CANDIDATES
    assert analysis.read_cache(analysis_project, 1).hit


def test_record_rejects_bad_candidates_without_writing(analysis_project: Path) -> None:
    with pytest.raises(analysis.CandidateError) as excinfo:
        analysis.record(analysis_project, 1, [{"score": 999, "title": "t", "reason": "r", "start": 0, "end": 5}])
    assert any("score" in v for v in excinfo.value.violations)
    assert not analysis.chunk_cache_path(analysis_project, 1).exists()


def test_record_is_a_no_op_on_a_cached_chunk(analysis_project: Path) -> None:
    analysis.record(analysis_project, 1, GOOD_CANDIDATES)
    different = [{"score": 5, "title": "other", "reason": "r", "start": 0, "end": 5}]
    result = analysis.record(analysis_project, 1, different)
    assert result.status == "cached"
    stored = json.loads(analysis.chunk_cache_path(analysis_project, 1).read_text(encoding="utf-8"))
    assert stored["items"] == GOOD_CANDIDATES  # never overwritten, never re-queried


# --- the dry-run plan ------------------------------------------------------------


def test_dry_run_lists_boundaries_hits_and_the_work_list(
    analysis_project: Path, analysis_settings
) -> None:
    _seed_cache(analysis_project, 2, {"chunk": 2, "model": None, "items": GOOD_CANDIDATES})
    plan = analysis.build_plan(analysis_project, duration=1000.0)
    assert plan["chunk_count"] == 3 and plan["cache_hits"] == 1 and plan["cache_misses"] == 2
    # a cached chunk never appears in the work list — it is never re-queried
    assert plan["to_query"] == ["chunk_001", "chunk_003"]
    first = plan["chunks"][0]
    assert (first["start"], first["end"], first["cache"]) == (90, 450, "miss")
    assert first["start_ts"] == "0:01:30"


def test_dry_run_reports_sequential_and_the_model_resolution(
    analysis_project: Path, analysis_settings
) -> None:
    plan = analysis.build_plan(analysis_project, duration=400.0)
    assert plan["parallelism"] == 1 and plan["sequential"] is True
    assert plan["model"] is None
    assert plan["model_note"] == "the Coding Agent's own model"

    plan = analysis.build_plan(analysis_project, duration=400.0, settings=_loaded(analysis_settings(model="nvidia/foo")))
    assert plan["model"] == "nvidia/foo" and plan["model_note"] == "nvidia/foo"
    assert plan["settings_path"] is not None


def test_dry_run_derives_duration_from_a_transcript_artifact(
    analysis_project: Path, analysis_settings
) -> None:
    (analysis_project / "transcript" / "X.words.json").write_text(
        json.dumps({"words": [{"start": 0, "end": 1200.5, "word": "x"}]}), encoding="utf-8"
    )
    plan = analysis.build_plan(analysis_project)
    assert plan["duration_source"] == "transcript artifact"
    assert plan["duration_sec"] == 1200.5


# --- the module entry (what the clip skill calls) --------------------------------


def test_module_entry_plan_prints_json(run_analysis, analysis_project: Path, analysis_settings) -> None:
    rc, out, err = run_analysis("plan", str(analysis_project), "--duration", "400")
    assert rc == 0, err
    plan = json.loads(out)
    assert plan["chunk_count"] == 1 and plan["to_query"] == ["chunk_001"]


def test_module_entry_validate_and_record(run_analysis, analysis_project: Path, analysis_settings, tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps(GOOD_CANDIDATES), encoding="utf-8")
    rc, out, err = run_analysis("validate", str(good))
    assert rc == 0 and out == ""

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"score": 500, "title": "t", "reason": "r", "start": 0, "end": 1}]), encoding="utf-8")
    rc, out, err = run_analysis("validate", str(bad))
    assert rc == 1 and "score" in err

    rc, out, err = run_analysis("record", str(analysis_project), "1", str(good))
    assert rc == 0 and "recorded" in out
    assert analysis.read_cache(analysis_project, 1).hit

    rc, out, err = run_analysis("record", str(analysis_project), "1", str(good))
    assert rc == 0 and "cache hit" in err  # never overwritten
