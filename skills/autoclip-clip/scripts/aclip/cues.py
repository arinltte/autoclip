"""The Cues rebuild — mlx-whisper's word fragments regrouped into caption-sized
cues (ticket #27; ported from the app era's ``src/cues.rs``, itself the Rust
port of ``tools/build_cues.py``).

mlx-whisper on long audio emits sub-second word-fragment segments; the rebuild
deterministically regroups the flattened words into caption-friendly cues
(karaoke captions are driven by word timings, Candidates snap to Cues):

* break at sentence-ending punctuation (after >= 1.2 s),
* break at a cue of 4.0 s or 38 characters (single-line 9:16 captions),
* break at long pauses (gap > 1.2 s).

The renders are byte-exact with the Python pipeline the app frozen its golden
fixtures against: cues json is ``json.dumps(cues, ensure_ascii=False, indent=1)``
(CPython float repr, key order start/end/text) and the cue SRT rounds half to
even (CPython ``round``). The tuning constants are ``build_cues.py``'s, verbatim.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

MAX_DUR = 4.0
MAX_CHARS = 38
MIN_DUR_ON_PUNCT = 1.2
PAUSE_BREAK = 1.2

_SENTENCE_END = {".", "!", "?", "…"}


@dataclass(frozen=True)
class Cue:
    """One caption cue — the ``{"start", "end", "text"}`` shape."""

    start: float
    end: float
    text: str


def fmt_srt_time(t: float) -> str:
    """``HH:MM:SS,mmm`` over CPython's round() (ties to even)."""
    ms = py_round(t * 1000.0)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def py_round(x: float) -> int:
    """CPython ``round(x)``: nearest, ties to even, on the double's exact value."""
    return int(round(x))


def rebuild_cues_from_segments(segments: Sequence[Mapping[str, Any]]) -> list[Cue]:
    """The full words.json ``segments`` array → cues.

    A segment without word timings is skipped (the app's
    ``seg.get("words", [])``); whitespace-only words never carry the timeline;
    a word without a timestamp is an error naming the word.
    """
    words: list[tuple[str, float, float]] = []
    for segment in segments:
        seg_words = segment.get("words")
        if not isinstance(seg_words, list):
            continue
        for word in seg_words:
            text = str(word.get("word", "")).strip()
            if not text:
                continue
            if "start" not in word or word.get("start") is None:
                raise ValueError(f"word {text!r} has no start timestamp")
            if "end" not in word or word.get("end") is None:
                raise ValueError(f"word {text!r} has no end timestamp")
            words.append((text, float(word["start"]), float(word["end"])))
    return regroup(words)


def rebuild_cues(result: Mapping[str, Any]) -> list[Cue]:
    """A full words.json (the mlx-whisper result) → cues."""
    segments = result.get("segments")
    if not isinstance(segments, list):
        raise ValueError('no "segments" array in the words json')
    return rebuild_cues_from_segments(segments)


def regroup(words: Sequence[tuple[str, float, float]]) -> list[Cue]:
    """The regroup loop, ported statement-for-statement from build_cues.py.

    ``cur_end`` is not reset by ``flush()`` — it is always re-set before a cue
    can be written.
    """
    cues: list[Cue] = []
    cur_words: list[str] = []
    cur_start: float | None = None
    cur_end = 0.0
    cur_chars = 0
    prev_end: float | None = None
    for word, start, end in words:
        if prev_end is not None and start - prev_end > PAUSE_BREAK:
            _flush(cues, cur_words, cur_start, cur_end)
            cur_words, cur_start, cur_chars = [], None, 0
        if cur_start is None:
            cur_start = start
        cur_words.append(word)
        cur_end = end
        cur_chars += len(word) + 1  # Python len(): code points
        ends_sentence = word and word[-1] in _SENTENCE_END
        duration = cur_end - (cur_start if cur_start is not None else start)
        if (ends_sentence and duration >= MIN_DUR_ON_PUNCT) or duration >= MAX_DUR or cur_chars >= MAX_CHARS:
            _flush(cues, cur_words, cur_start, cur_end)
            cur_words, cur_start, cur_chars = [], None, 0
        prev_end = end
    _flush(cues, cur_words, cur_start, cur_end)
    return cues


def _flush(
    cues: list[Cue],
    cur_words: Sequence[str],
    cur_start: float | None,
    cur_end: float,
) -> None:
    """Close the open cue; an open cue without a start is a defect (never a drop)."""
    if cur_words:
        if cur_start is None:
            raise ValueError("an open cue always has a start")
        cues.append(Cue(cur_start, cur_end, " ".join(cur_words)))


def cue_duration_stats(cues: Sequence[Cue]) -> tuple[float, float, float] | None:
    """``(min, median, max)`` of the cue durations — the lower-middle median
    (``sorted(lens)[len(lens) // 2]``). None = a wordless transcript."""
    if not cues:
        return None
    lens = sorted(cue.end - cue.start for cue in cues)
    return lens[0], lens[len(lens) // 2], lens[-1]


def render_cues_json(cues: Sequence[Cue]) -> str:
    """The exact bytes ``json.dumps(cues, ensure_ascii=False, indent=1)`` writes."""
    return json.dumps(
        [{"start": cue.start, "end": cue.end, "text": cue.text} for cue in cues],
        ensure_ascii=False,
        indent=1,
    )


def render_cues_srt(cues: Sequence[Cue]) -> str:
    """The clean cue SRT — it replaces the fragmented segment SRT."""
    return "\n".join(
        f"{i + 1}\n{fmt_srt_time(cue.start)} --> {fmt_srt_time(cue.end)}\n{cue.text}\n"
        for i, cue in enumerate(cues)
    )


def non_finite_to_none(obj: Any) -> Any:
    """Non-finite floats → None: mlx-whisper reports NaN avg_logprob on
    no-speech segments and ``json.dumps`` writes those as the non-standard
    NaN/Infinity literals, which strict JSON readers reject (the
    fix/transcribe-nan-words-json regression, pinned by tests)."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {key: non_finite_to_none(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [non_finite_to_none(item) for item in obj]
    return obj
