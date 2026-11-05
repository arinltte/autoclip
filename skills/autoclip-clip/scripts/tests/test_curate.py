"""Deterministic curation rules, one test per rule plus ties and chains (ticket #31)."""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from aclip import curate


def cand(score: int, start: float, end: float, title: str = "t") -> dict[str, Any]:
    return {"score": score, "title": title, "reason": "r", "start": start, "end": end}


RULES = curate.CurationSettings()  # the frozen defaults: 30/90 bounds, intro 90, top 10


def titles(selection: list) -> list[str]:
    return [c["title"] for c in selection]


def test_candidates_outside_the_bounds_are_dropped_at_the_boundaries() -> None:
    picked = curate.select(
        [cand(50, 100, 130, "exact-min"), cand(50, 200, 290, "exact-max"),
         cand(50, 300, 329.5, "too-short"), cand(50, 400, 491, "too-long")],
        RULES,
    )
    assert titles(picked) == ["exact-min", "exact-max"]


def test_candidates_starting_inside_the_intro_are_skipped() -> None:
    picked = curate.select(
        [cand(50, 0, 60, "cold-open"), cand(50, 89, 130, "starts-just-before"),
         cand(50, 90, 130, "starts-at-the-boundary")],
        RULES,
    )
    assert titles(picked) == ["starts-at-the-boundary"]


def test_no_floor_by_default_and_a_floor_when_set() -> None:
    low = cand(1, 100, 160, "floorless-kept")
    assert curate.select([low], RULES) == [low]  # min_score: null keeps everything
    floored = curate.CurationSettings(min_score=50.0)
    assert curate.select([low], floored) == []
    assert curate.select([cand(50, 100, 160)], floored) == [cand(50, 100, 160)]


def test_overlap_merges_highest_score_first_and_absorbs() -> None:
    high = cand(80, 100, 160, "high")
    low = cand(70, 130, 200, "low")  # overlaps high, score loses
    picked = curate.select([low, high], RULES)
    assert picked == [high]  # the keeper's span is unchanged; the absorbed one never counts


def test_touching_spans_are_neighbours_not_duplicates() -> None:
    first, second = cand(80, 100, 130, "first"), cand(70, 130, 160, "second")
    assert titles(curate.select([first, second], RULES)) == ["first", "second"]


def test_overlap_chains_compare_against_kept_not_survivors() -> None:
    a = cand(80, 100, 160, "A")
    b = cand(70, 130, 220, "B")  # overlaps A -> absorbed
    c = cand(60, 170, 240, "C")  # overlaps absorbed B, not kept A -> kept
    picked = curate.select([c, b, a], RULES)
    assert titles(picked) == ["A", "C"]


def test_top_n_takes_the_best_and_stops() -> None:
    three = [
        cand(90, 400, 460, "best"),
        cand(80, 500, 560, "second"),
        cand(70, 600, 660, "third"),
    ]
    assert titles(curate.select(three, curate.CurationSettings(target_count=2))) == ["best", "second"]


def test_ties_break_by_start_time_in_the_output_order() -> None:
    early, late = cand(70, 200, 260, "early"), cand(70, 100, 160, "late")
    picked = curate.select([early, late], RULES)
    assert [c["start"] for c in picked] == [100, 200]  # same score -> earlier first


def test_selection_is_ordered_best_first() -> None:
    picked = curate.select(
        [cand(60, 200, 260), cand(90, 400, 460), cand(75, 600, 660)], RULES
    )
    assert [c["score"] for c in picked] == [90, 75, 60]


def test_select_does_not_mutate_its_input() -> None:
    candidates = [cand(80, 130, 200, "low"), cand(90, 100, 160, "high")]
    snapshot = [dict(c) for c in candidates]
    curate.select(candidates, RULES)
    assert candidates == snapshot


def test_select_from_settings_reads_the_tracked_defaults(analysis_settings) -> None:
    """Through the real parser: bounds 30/90, intro 90, top 10, no floor."""
    picked = curate.select_from_settings(
        [cand(50, 100, 130, "kept"), cand(50, 0, 60, "intro"), cand(50, 700, 800, "too-long")]
    )
    assert titles(picked) == ["kept"]


def test_settings_override_moves_the_rules(analysis_settings) -> None:
    analysis_settings(target_count="1", clip_max_sec="60")
    picked = curate.select_from_settings(
        [cand(50, 100, 160, "90s-clip"), cand(80, 200, 260, "60s-clip"), cand(40, 300, 320, "20s-clip")]
    )
    assert titles(picked) == ["60s-clip"]  # only the in-bounds 60 s clip survives top-1


# --- the deterministic selection seam the clip skill calls (#33) ---------------------


def _seed_chunk(project: Path, index: int, items: list[dict]) -> None:
    (project / "analyze").mkdir(parents=True, exist_ok=True)
    (project / "analyze" / f"chunk_{index:03d}.json").write_text(
        json.dumps({"chunk": index, "model": None, "items": items}), encoding="utf-8"
    )


def test_build_selection_orders_and_renumbers(analysis_project: Path, analysis_settings) -> None:
    _seed_chunk(analysis_project, 1, [cand(50, 200, 260, "later"), cand(80, 100, 160, "better")])
    _seed_chunk(analysis_project, 2, [cand(70, 400, 460, "third")])
    payload = curate.build_selection(analysis_project)
    assert payload["selection_count"] == 3 and payload["candidate_count"] == 3
    assert [clip["id"] for clip in payload["clips"]] == ["clip_001", "clip_002", "clip_003"]
    assert [clip["score"] for clip in payload["clips"]] == [80, 70, 50]
    assert payload["clips"][0]["generated_title"] == "better"  # chunk 1's 80 ranks first
    assert payload["clips"][1]["generated_title"] == "third"


def test_build_selection_drops_out_of_bounds_and_intro(analysis_project: Path, analysis_settings) -> None:
    _seed_chunk(analysis_project, 1, [cand(80, 0, 60, "intro"), cand(80, 700, 800, "too-long"), cand(80, 100, 160, "kept")])
    payload = curate.build_selection(analysis_project)
    assert [clip["generated_title"] for clip in payload["clips"]] == ["kept"]


def test_build_selection_stops_at_the_last_cached_chunk(analysis_project: Path, analysis_settings) -> None:
    _seed_chunk(analysis_project, 1, [cand(80, 100, 160, "a")])
    payload = curate.build_selection(analysis_project)
    assert payload["selection_count"] == 1  # a gap after chunk 1 ends the scan


def test_write_selection_is_the_render_contract(analysis_project: Path, analysis_settings) -> None:
    _seed_chunk(analysis_project, 1, [cand(80, 100, 160, "the one")])
    payload = curate.build_selection(analysis_project)
    path = curate.write_selection(analysis_project, payload)
    assert path.name == "selection.json"
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    from aclip import render

    loaded = render.load_selection(analysis_project)
    assert loaded[0]["generated_title"] == "the one"


# --- the full analysis and its continuation (#38) ---------------------------------


def _seed_many(project: Path) -> None:
    """Twenty ranked candidates: the top ten are in bounds, ten more wait."""
    items = [cand(90, 100 + 40 * i, 140 + 40 * i, f"clip-{i:02d}") for i in range(20)]
    _seed_chunk(project, 1, items)


def test_the_top_count_is_the_default_and_the_rest_stays_available(
    analysis_project: Path, analysis_settings
) -> None:
    _seed_many(analysis_project)
    payload = curate.build_selection(analysis_project)
    assert payload["selection_count"] == 10  # target_count unchanged
    assert [clip["rank"] for clip in payload["clips"]] == list(range(1, 11))
    rows = payload["inventory"]["candidates"]
    assert len(rows) == 20
    assert [row["disposition"] for row in rows[:10]] == ["selected"] * 10
    assert [row["disposition"] for row in rows[10:]] == ["available"] * 10


def test_the_inventory_carries_a_disposition_for_every_candidate(
    analysis_project: Path, analysis_settings
) -> None:
    _seed_chunk(
        analysis_project,
        1,
        [
            cand(80, 100, 160, "kept"),
            cand(80, 130, 190, "merged"),  # overlaps kept, lower rank
            cand(80, 0, 60, "intro"),
            cand(80, 700, 800, "too-long"),
        ],
    )
    rows = curate.build_selection(analysis_project)["inventory"]["candidates"]
    by_title = {row["title"]: row for row in rows}
    assert by_title["kept"]["disposition"] == "selected"
    assert by_title["merged"]["disposition"] == "merged"
    assert by_title["merged"]["merged_into"]["start"] == 100
    assert by_title["intro"]["disposition"] == "dropped_intro"
    assert by_title["too-long"]["disposition"] == "dropped_out_of_bounds"


def test_rank_candidates_is_best_first_across_every_disposition() -> None:
    rows = curate.rank_candidates(
        [
            cand(60, 100, 160, "low"),
            cand(95, 700, 800, "too-long-but-best"),
            cand(80, 300, 360, "mid"),
        ],
        curate.CurationSettings(),
    )
    assert [row["title"] for row in rows] == ["too-long-but-best", "mid", "low"]
    assert rows[0]["disposition"] == "dropped_out_of_bounds"
    assert rows[0]["rank"] is None


def test_select_takes_the_window_after_the_cut(analysis_project: Path, analysis_settings) -> None:
    _seed_many(analysis_project)
    first = curate.select_from_settings(
        [cand(80, 100, 160, "a"), cand(70, 300, 360, "b"), cand(60, 500, 560, "c")]
    )
    # the window form is the continuation: skip the first, take the next
    window = curate.select(
        [cand(80, 100, 160, "a"), cand(70, 300, 360, "b"), cand(60, 500, 560, "c")],
        curate.CurationSettings(target_count=1),
        skip=1,
    )
    assert [c["title"] for c in window] == ["b"]
    assert [c["title"] for c in first[:2]] == ["a", "b"]


def test_build_selection_keeps_the_ranking_ranks_when_skipping(
    analysis_project: Path, analysis_settings
) -> None:
    _seed_many(analysis_project)
    payload = curate.build_selection(analysis_project, skip=10)
    assert payload["skip"] == 10
    assert [clip["rank"] for clip in payload["clips"]] == list(range(11, 21))
    assert [clip["id"] for clip in payload["clips"][:2]] == ["clip_011", "clip_012"]


def test_write_inventory_is_the_full_analysis_artifact(
    analysis_project: Path, analysis_settings
) -> None:
    _seed_many(analysis_project)
    payload = curate.build_selection(analysis_project)
    path = curate.write_inventory(analysis_project, payload)
    assert path.name == "candidates.json"
    rows = json.loads(path.read_text(encoding="utf-8"))["candidates"]
    assert len(rows) == 20 and rows[0]["disposition"] == "selected"


def test_the_module_entry_writes_both_artifacts_and_accepts_skip(
    analysis_project: Path, analysis_settings, monkeypatch
) -> None:
    _seed_many(analysis_project)
    monkeypatch.setattr(
        sys, "argv", ["aclip.curate", str(analysis_project), "--skip", "10"]
    )
    assert curate.main() == 0
    selection = json.loads((analysis_project / "analyze" / "selection.json").read_text(encoding="utf-8"))
    assert [clip["rank"] for clip in selection["clips"]] == list(range(11, 21))
    assert (analysis_project / "analyze" / "candidates.json").is_file()


def test_the_module_entry_accepts_skip_equals(analysis_project: Path, analysis_settings, monkeypatch) -> None:
    _seed_many(analysis_project)
    monkeypatch.setattr(sys, "argv", ["aclip.curate", str(analysis_project), "--skip=10"])
    assert curate.main() == 0
    selection = json.loads((analysis_project / "analyze" / "selection.json").read_text(encoding="utf-8"))
    assert selection["clips"][0]["rank"] == 11


def test_the_module_entry_takes_the_project_dir_from_argv(tmp_path, monkeypatch) -> None:
    """``python -m aclip.curate <project_dir>`` must read argv[1], not argv[2].

    The clip skill's step 7 invokes exactly this form. The golden run (#36)
    found the entry reading ``sys.argv[2:]`` — the documented invocation then
    printed usage and wrote no selection, stopping the run at curation.
    """
    import json
    import sys

    project = tmp_path / "_Ia-G4KKKks"
    (project / "analyze").mkdir(parents=True)
    (project / "analyze" / "chunk_001.json").write_text(
        json.dumps({"chunk": 1, "model": None,
                    "items": [cand(80, 100, 160, "a"), cand(70, 200, 260, "b")]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["aclip.curate", str(project)])

    assert curate.main() == 0

    selection = json.loads((project / "analyze" / "selection.json").read_text(encoding="utf-8"))
    assert selection["selection_count"] == 2
    assert [clip["generated_title"] for clip in selection["clips"]] == ["a", "b"]
