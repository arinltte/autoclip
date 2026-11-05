"""``aclip compare`` — the two transcript tracks' disagreement report (ticket #26).

The Whisper Track (word timings, the caption source) and the YouTube Track
(wording reference) are compared with a filler-stripped token-agreement metric.
At/above the settings threshold (0.85, calibrated — the real ``X5HIcE2BRuc``
pair measures ~0.918 filler-stripped; the app-era API captions were wrong on
Malaysian proper nouns there), the report records "adopt whisper" and the run
continues silently. Below threshold it carries the ONE routine question of the
pipeline — ``ask the user: A whisper / B youtube / C both`` — with sample
divergences attached, for the clip skill to ask once and remember as
``prefer:`` (ADR 0007 / spec decision 7). No captions (the empty marker from
#25) means whisper-only: a skipped report, exit 0, no question.

The metric — token agreement via ``difflib.SequenceMatcher`` with autojunk
DISABLED over ``\\b[\\w']+\\b`` tokens. Autojunk treats tokens popular in the
second sequence as noise, which distorts agreement on 16k-token prose; disabled
is the honest token-agreement reading. Filler-stripped repeats the metric on
the streams minus the filler tokens listed below. Both numbers are pinned by
the golden fixture — a metric change that moves them is a deliberate act.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from aclip import settings as settings_module
from aclip import transcript_fetch
from aclip.analysis import duration_from_srt

# The pure fillers stripped before the "filler-stripped" agreement number.
# Discourse markers ("you know", "i mean", "like") are NOT stripped: they are
# real words whose presence legitimately signals a wording difference.
FILLER_TOKENS = frozenset({"um", "uh", "erm", "er", "mm", "hmm", "mhm", "eh", "ah", "oh"})

# Whisper's own documented decode thresholds (mlx-whisper mirrors them): a
# segment above no-speech 0.6 or below average-log-prob -1.0 is a
# low-confidence span — the "flagged span" band (spec decision 11).
NO_SPEECH_PROB_BAND = 0.6
AVG_LOGPROB_BAND = -1.0

_SRT_CUE = re.compile(
    r"(?m)^\s*\d+\s*\n\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n(.*?)(?=\n\s*\n|\n\s*\d+\s*\n|\Z)",
    re.S,
)
_TOKEN = re.compile(r"\b[\w']+\b")


def _seconds(h: int, m: int, s: int, ms: int) -> float:
    return h * 3600 + m * 60 + s + ms / 1000.0


def parse_srt(text: str) -> list[dict[str, Any]]:
    """The SRT cues as ``{index, start, end, text}`` (tags stripped)."""
    cues: list[dict[str, Any]] = []
    for n, match in enumerate(_SRT_CUE.finditer(text), start=1):
        body = re.sub(r"<[^>]+>", "", match.group(9)).replace("\n", " ").strip()
        cues.append(
            {
                "index": n,
                "start": _seconds(*(int(g) for g in match.group(1, 2, 3, 4))),
                "end": _seconds(*(int(g) for g in match.group(5, 6, 7, 8))),
                "text": body,
            }
        )
    return cues


def tokenize(text: str) -> list[str]:
    """Lowercased word tokens (possessives stay attached: "what's" is one)."""
    return _TOKEN.findall(text.lower())


def strip_fillers(tokens: Sequence[str]) -> list[str]:
    return [token for token in tokens if token not in FILLER_TOKENS]


def similarity(left: Sequence[str], right: Sequence[str]) -> float:
    """Token agreement: SequenceMatcher ratio, autojunk disabled (prose-safe)."""
    import difflib  # stdlib, imported late to keep this module's surface small

    return difflib.SequenceMatcher(None, list(left), list(right), autojunk=False).ratio()


def track_agreement(whisper_text: str, youtube_text: str) -> dict[str, float]:
    """Raw and filler-stripped agreement between the two tracks' text."""
    left_all, right_all = tokenize(whisper_text), tokenize(youtube_text)
    return {
        "raw": similarity(left_all, right_all),
        "filler_stripped": similarity(strip_fillers(left_all), strip_fillers(right_all)),
        "whisper_tokens": len(left_all),
        "youtube_tokens": len(right_all),
    }


def per_minute_blocks(
    whisper_cues: Sequence[Mapping[str, Any]],
    youtube_lines: Sequence[Mapping[str, Any]],
    threshold: float,
) -> list[dict[str, Any]]:
    """Where they diverge: per-minute token agreement between the tracks."""
    def buckets(entries: Sequence[Mapping[str, Any]]) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for entry in entries:
            minute = int(float(entry["start"]) // 60)
            out.setdefault(minute, []).extend(tokenize(entry.get("text", "")))
        return out

    left, right = buckets(whisper_cues), buckets(youtube_lines)
    blocks = []
    for minute in sorted(set(left) | set(right)):
        left_tokens, right_tokens = left.get(minute, []), right.get(minute, [])
        matched = similarity(left_tokens, right_tokens) if left_tokens and right_tokens else 0.0
        blocks.append(
            {
                "minute": minute,
                "start_ts": f"{minute // 60}:{minute % 60:02d}",
                "whisper_tokens": len(left_tokens),
                "youtube_tokens": len(right_tokens),
                "similarity": matched,
                "diverged": matched < threshold,
            }
        )
    return blocks


def flagged_spans(
    segments: Sequence[Mapping[str, Any]],
    no_speech_band: float = NO_SPEECH_PROB_BAND,
    avg_logprob_band: float = AVG_LOGPROB_BAND,
) -> dict[str, Any]:
    """Low-confidence whisper spans: the "music-only / no speech" stretches.

    A segment is flagged at/above ``no_speech_band`` no-speech probability OR
    below ``avg_logprob_band`` average log-probability (whisper's own decode
    thresholds — the documented band). Adjacent flagged segments merge into one
    span; those spans are carried without captions and listed in the end-of-run
    report (spec decision 11 — the ~10%-of-duration interrupt is the clip
    skill's, #33, not this module's).
    """
    flagged: list[tuple[float, float, dict[str, float]]] = []
    for segment in segments:
        no_speech = float(segment.get("no_speech_prob", 0.0) or 0.0)
        avg_logprob = segment.get("avg_logprob")
        low = no_speech >= no_speech_band
        if avg_logprob is not None and float(avg_logprob) < avg_logprob_band:
            low = True
        if low:
            flagged.append((float(segment["start"]), float(segment["end"]), {"no_speech_prob": no_speech, "avg_logprob": avg_logprob if avg_logprob is None else float(avg_logprob)}))
    spans: list[dict[str, Any]] = []
    for start, end, why in flagged:
        if spans and start <= spans[-1]["end"] + 0.5:
            spans[-1]["end"] = max(spans[-1]["end"], end)
            spans[-1]["segments"] += 1
            spans[-1]["no_speech_prob"] = max(spans[-1]["no_speech_prob"], why["no_speech_prob"])
            if why["avg_logprob"] is not None:
                previous = spans[-1]["avg_logprob"]
                spans[-1]["avg_logprob"] = why["avg_logprob"] if previous is None else min(previous, why["avg_logprob"])
            continue
        spans.append({"start": start, "end": end, "segments": 1, **why})
    for span in spans:
        span["duration_sec"] = round(span["end"] - span["start"], 3)
    total = sum(span["duration_sec"] for span in spans)
    return {
        "spans": spans,
        "span_count": len(spans),
        "flagged_sec": round(total, 3),
        "band": {"no_speech_prob": no_speech_band, "avg_logprob": avg_logprob_band},
    }


def sample_divergences(
    whisper_cues: Sequence[Mapping[str, Any]],
    youtube_lines: Sequence[Mapping[str, Any]],
    count: int = 5,
) -> list[dict[str, Any]]:
    """A few concrete disagreements for the ask-the-user question (#26)."""
    import difflib  # stdlib

    left_all, right_all = [], []
    for cue in whisper_cues:
        left_all.extend((float(cue["start"]), token) for token in tokenize(cue.get("text", "")))
    for line in youtube_lines:
        right_all.extend((float(line["start"]), token) for token in tokenize(line.get("text", "")))
    left_tokens = [token for _, token in left_all]
    right_tokens = [token for _, token in right_all]
    starts_left = [start for start, _ in left_all]
    starts_right = [start for start, _ in right_all]
    samples: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(None, left_tokens, right_tokens, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        samples.append(
            {
                "kind": tag,
                "minute": int(starts_left[i1] // 60) if i1 < len(starts_left) else int(starts_right[j1] // 60),
                "start_ts": starts_left[i1] if i1 < len(starts_left) else starts_right[j1],
                "whisper": " ".join(left_tokens[i1:i2]),
                "youtube": " ".join(right_tokens[j1:j2]),
            }
        )
        if len(samples) >= count:
            break
    return samples


def compare_report_path(project_dir: Path | str) -> Path:
    """``transcript/compare_report.json`` — where the clip skill reads step 5."""
    return Path(project_dir) / "transcript" / "compare_report.json"


def build_report(
    project_dir: Path | str,
    video_id: str,
    settings: Any = None,
) -> dict[str, Any]:
    """The comparison report dict; zero model calls, no network."""
    resolved = settings if settings is not None else settings_module.load_settings()
    threshold = float(resolved.similarity_threshold)
    transcript_dir = Path(project_dir) / "transcript"
    srt_paths = sorted(transcript_dir.glob("*.srt"))
    track_path = transcript_fetch.youtube_track_path(project_dir)

    if not srt_paths:
        raise FileNotFoundError(
            f"no Whisper Track (*.srt) in {transcript_dir} — run aclip transcribe first"
        )
    if not track_path.is_file():
        raise FileNotFoundError(
            f"{track_path} is missing — run aclip transcript-fetch first"
        )
    track = json.loads(track_path.read_text(encoding="utf-8"))
    # The media duration the ~10% interrupt is measured against (decision 11 —
    # the fraction is the clip skill's to act on; this module only reports it).
    duration = round(duration_from_srt(srt_paths[0]), 3)

    if track.get("empty"):
        return {
            "video_id": video_id,
            "comparison": "skipped",
            "reason": "no YouTube captions — whisper-only (spec transcript policy)",
            "language_code": track.get("language_code"),
            "whisper_track": str(srt_paths[0]),
            "duration_sec": duration,
            "similarity_raw": None,
            "similarity_filler_stripped": None,
            "flagged_fraction": None,
            "threshold": threshold,
            "decision": "whisper_only",
        }

    whisper_cues = parse_srt(srt_paths[0].read_text(encoding="utf-8"))
    youtube_lines = track["lines"]
    agreement = track_agreement(
        " ".join(cue["text"] for cue in whisper_cues),
        " ".join(line["text"] for line in youtube_lines),
    )
    blocks = per_minute_blocks(whisper_cues, youtube_lines, threshold)
    diverged = [block for block in blocks if block["diverged"]]

    segments_path = transcript_dir / f"{srt_paths[0].stem}.words.json"
    flagged: dict[str, Any] = {"spans": [], "span_count": 0, "flagged_sec": 0.0}
    if segments_path.is_file():
        payload = json.loads(segments_path.read_text(encoding="utf-8"))
        flagged = flagged_spans(payload.get("segments", []))

    used = agreement["filler_stripped"]
    report: dict[str, Any] = {
        "video_id": video_id,
        "comparison": "compared",
        "language_code": track.get("language_code", ""),
        "whisper_track": str(srt_paths[0]),
        "youtube_track": str(track_path),
        "threshold": threshold,
        "similarity_raw": round(agreement["raw"], 4),
        "similarity_filler_stripped": round(agreement["filler_stripped"], 4),
        "whisper_tokens": agreement["whisper_tokens"],
        "youtube_tokens": agreement["youtube_tokens"],
        "per_minute_blocks": blocks,
        "diverged_minutes": [block["minute"] for block in diverged],
        "flagged_spans": flagged,
        "duration_sec": duration,
        "flagged_fraction": round(flagged["flagged_sec"] / duration, 4) if duration else None,
        "decision": "adopt_whisper" if used >= threshold else "ask",
    }
    if used < threshold:
        report["ask"] = {
            "question": "ask the user: A whisper / B youtube / C both",
            "options": ["A whisper", "B youtube", "C both"],
            "remember_as": "prefer:",
            "sample_divergences": sample_divergences(whisper_cues, youtube_lines, count=5),
        }
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """``aclip compare <Video Id>`` — writes the report, returns the exit code."""
    if argv is None:
        argv = []
    argv = list(argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: aclip compare <Video Id>", file=sys.stderr)
        return 0 if argv and argv[0] in {"-h", "--help"} else 2
    video_id = argv[0]
    project_dir = transcript_fetch.project_dir_for(video_id)
    try:
        report = build_report(project_dir, video_id)
    except FileNotFoundError as exc:
        print(f"compare: {exc}", file=sys.stderr)
        return 1
    path = compare_report_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(path))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    sys.exit(main())
