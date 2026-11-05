"""The track comparison: golden metric pin, thresholds, flags, ask payload (#26).

The golden pair is real and already-measured (the stored ``X5HIcE2BRuc``
whisper SRT + its YouTube track): the filler-stripped number reproduces the
calibration session's 0.918 exactly; the raw number (0.8957 here) sits 0.005
from the session's 0.891 — an autojunk/tokenizer detail, same side of 0.85,
decision unchanged. A metric change that moves EITHER number is a deliberate
act: they are pinned to the fourth decimal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aclip import compare

FIXTURES = Path(__file__).resolve().parent / "fixtures"

HOUR_SRT = """1
00:00:00,000 --> 00:00:02,000
Hello there friends

2
00:00:02,000 --> 00:00:04,000
welcome back to the show
"""


# --- the golden metric pin -------------------------------------------------------


def test_golden_pair_pins_the_metric() -> None:
    """The one sacred number: the real pair's measured agreement."""
    cues = compare.parse_srt((FIXTURES / "golden_X5HIcE2BRuc_whisper.srt").read_text(encoding="utf-8"))
    track = json.loads((FIXTURES / "golden_X5HIcE2BRuc_youtube.json").read_text(encoding="utf-8"))
    agreement = compare.track_agreement(
        " ".join(cue["text"] for cue in cues), " ".join(line["text"] for line in track["lines"])
    )
    assert round(agreement["raw"], 4) == 0.8957
    assert round(agreement["filler_stripped"], 4) == 0.9180  # the session's 0.918, exactly
    # token counts are part of the pin: a fixture change is visible immediately
    assert (agreement["whisper_tokens"], agreement["youtube_tokens"]) == (16264, 18141)


def test_golden_pair_adopts_whisper_at_the_calibrated_threshold() -> None:
    cues = compare.parse_srt((FIXTURES / "golden_X5HIcE2BRuc_whisper.srt").read_text(encoding="utf-8"))
    track = json.loads((FIXTURES / "golden_X5HIcE2BRuc_youtube.json").read_text(encoding="utf-8"))
    blocks = compare.per_minute_blocks(cues, track["lines"], 0.85)
    diverged = [block["minute"] for block in blocks if block["diverged"]]
    # most minutes agree; the diverged ones are where the API captions mangled
    # Malaysian proper nouns — localisation works
    assert len(blocks) == 99 and 0 < len(diverged) < len(blocks)
    first = compare.sample_divergences(cues, track["lines"], count=1)[0]
    assert first["whisper"] == "ong kien" and first["youtube"] == "anen"


# --- parsing ---------------------------------------------------------------------


def test_parse_srt_cues_and_tags() -> None:
    cues = compare.parse_srt(HOUR_SRT)
    assert [(c["start"], c["end"]) for c in cues] == [(0.0, 2.0), (2.0, 4.0)]
    assert cues[0]["text"] == "Hello there friends"


def test_fillers_strip_but_discourse_markers_stay() -> None:
    assert compare.strip_fillers(["um", "you", "know", "the"]) == ["you", "know", "the"]


# --- the flagged-span band -------------------------------------------------------


def _segment(start: float, end: float, no_speech: float, avg_logprob: float) -> dict:
    return {"start": start, "end": end, "no_speech_prob": no_speech, "avg_logprob": avg_logprob}


def test_flagged_band_boundaries() -> None:
    quiet = compare.flagged_spans([_segment(0, 2, 0.6, -0.5), _segment(2, 4, 0.5, -1.0)])
    assert quiet["span_count"] == 1  # the 0.6 crosses the at-or-above band
    assert quiet["spans"][0]["start"] == 0.0

    clean = compare.flagged_spans([_segment(0, 2, 0.5, -0.5), _segment(2, 4, 0.3, -0.9)])
    assert clean["span_count"] == 0 and clean["flagged_sec"] == 0.0

    by_logprob = compare.flagged_spans([_segment(10, 12, 0.0, -1.01)])
    assert by_logprob["span_count"] == 1 and by_logprob["spans"][0]["avg_logprob"] == -1.01


def test_adjacent_flagged_segments_merge_into_one_span() -> None:
    segments = [_segment(0, 2, 0.9, -0.1), _segment(2.2, 4, 0.8, -0.1), _segment(10, 12, 0.7, -0.1)]
    flagged = compare.flagged_spans(segments)
    assert flagged["span_count"] == 2
    assert (flagged["spans"][0]["start"], flagged["spans"][0]["end"]) == (0.0, 4.0)
    assert flagged["spans"][0]["segments"] == 2


# --- the report ------------------------------------------------------------------


def _make_project(tmp_path: Path, *, empty_marker: bool = False, words: list | None = None) -> Path:
    project = tmp_path / "VID"
    transcript = project / "transcript"
    transcript.mkdir(parents=True)
    (transcript / "VID_hi.srt").write_text(HOUR_SRT, encoding="utf-8")
    if empty_marker:
        (transcript / "youtube_track.json").write_text(
            json.dumps({"video_id": "VID", "empty": True, "lines": [], "language_code": "en"}), encoding="utf-8"
        )
    else:
        (transcript / "youtube_track.json").write_text(
            json.dumps(
                {
                    "video_id": "VID",
                    "language_code": "en",
                    "lines": [
                        {"text": "Hello there friends", "start": 0.0, "duration": 2.0},
                        {"text": "welcome back to the show", "start": 2.0, "duration": 2.0},
                    ],
                }
            ),
            encoding="utf-8",
        )
    if words is not None:
        (transcript / "VID_hi.words.json").write_text(json.dumps({"segments": words}), encoding="utf-8")
    return project


def test_report_adopts_whisper_on_an_agreeing_pair(tmp_path: Path, analysis_settings) -> None:
    project = _make_project(tmp_path)
    report = compare.build_report(project, "VID")
    assert report["comparison"] == "compared"
    assert report["decision"] == "adopt_whisper"
    assert report["similarity_filler_stripped"] >= report["threshold"] == 0.85
    assert "ask" not in report


def test_report_asks_below_the_threshold_with_samples(tmp_path: Path, analysis_settings) -> None:
    project = _make_project(tmp_path)
    (project / "transcript" / "youtube_track.json").write_text(
        json.dumps(
            {
                "video_id": "VID",
                "language_code": "en",
                "lines": [
                    {"text": "something else entirely", "start": 0.0, "duration": 2.0},
                    {"text": "and more different words", "start": 2.0, "duration": 2.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    report = compare.build_report(project, "VID")
    assert report["decision"] == "ask"
    ask = report["ask"]
    assert ask["question"] == "ask the user: A whisper / B youtube / C both"
    assert ask["options"] == ["A whisper", "B youtube", "C both"]
    assert ask["remember_as"] == "prefer:"
    assert ask["sample_divergences"]


def test_report_is_skipped_without_captions(tmp_path: Path) -> None:
    project = _make_project(tmp_path, empty_marker=True)
    report = compare.build_report(project, "VID")
    assert report["comparison"] == "skipped" and report["decision"] == "whisper_only"
    assert report["similarity_raw"] is None


def test_report_carries_flagged_spans(tmp_path: Path) -> None:
    project = _make_project(tmp_path, words=[_segment(1, 3, 0.95, -0.2), _segment(3.2, 5, 0.99, -0.2)])
    report = compare.build_report(project, "VID")
    assert report["flagged_spans"]["span_count"] == 1
    assert report["flagged_spans"]["spans"][0]["duration_sec"] == 4.0  # merged 1–5 s


def test_threshold_follows_settings(tmp_path: Path, analysis_settings) -> None:
    analysis_settings(similarity_threshold="0.1")
    project = _make_project(tmp_path)
    report = compare.build_report(project, "VID")
    # even a weak pair clears a 0.1 floor: settings drive the decision
    assert report["threshold"] == 0.1 and report["decision"] == "adopt_whisper"


def test_the_core_is_stdlib_only() -> None:
    """No third-party import rides along with the comparison module."""
    import sys

    assert "youtube_transcript_api" not in sys.modules


def test_report_carries_the_flagged_fraction_the_interrupt_needs(tmp_path: Path) -> None:
    """Interrupt #4 (decision 11) compares the flagged fraction against ~10% of
    the duration, so the report must carry both numbers."""
    project = _make_project(tmp_path, words=[_segment(1, 3, 0.95, -0.2), _segment(3.2, 5, 0.99, -0.2)])
    report = compare.build_report(project, "VID")
    assert report["duration_sec"] == 4.0  # the last whisper cue's end
    assert report["flagged_fraction"] == 1.0  # 4 s flagged of 4 s — over the ~10% band
    short = _make_project(tmp_path / "other", words=[_segment(0, 1, 0.95, -0.2)])
    short_report = compare.build_report(short, "VID")
    assert short_report["duration_sec"] == 4.0
    assert short_report["flagged_fraction"] == 0.25  # 1 s of 4 s — under it


def test_the_whisper_only_report_carries_the_duration_too(tmp_path: Path) -> None:
    project = _make_project(tmp_path, empty_marker=True)
    report = compare.build_report(project, "VID")
    assert report["comparison"] == "skipped"
    assert report["duration_sec"] == 4.0
    assert report["flagged_fraction"] is None
