#!/usr/bin/env python3
"""
Transcribe a video to SRT (with word-level timings) using mlx-whisper.

Usage:
    .venv/bin/python tools/transcribe.py <video_path> [--model <hf_repo>] [--language <lang>]

Outputs (in transcripts/):
    <video_stem>.words.json   full whisper result incl. word timestamps (for karaoke captions)
    <video_stem>.srt          segment-level SRT (standard, fed to autoclip via --srt)

Model downloads land in models/hf-cache (project-local, see tools/env.sh).
"""
import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
TRANSCRIPTS_DIR = PROJECT_DIR / "transcripts"
FFMPEG = os.environ.get("FFMPEG_BIN", "/opt/homebrew/bin/ffmpeg")


def probe_duration(video: Path) -> float:
    out = subprocess.run(
        ["/opt/homebrew/bin/ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def fmt_srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def json_safe(obj):
    """Non-finite floats -> None: mlx-whisper reports NaN avg_logprob on
    no-speech segments and Python's json.dumps writes those as the
    non-standard NaN/Infinity literals, which strict JSON readers (serde_json)
    reject. Emit standard JSON instead."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    return obj


def write_srt(segments, path: Path) -> None:
    lines = []
    for i, seg in enumerate(segments, 1):
        text = seg["text"].strip()
        if not text:
            continue
        lines.append(f"{i}\n{fmt_srt_time(seg['start'])} --> {fmt_srt_time(seg['end'])}\n{text}\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--model", default="mlx-community/whisper-large-v3-turbo")
    ap.add_argument("--language", default=None, help="ISO code; default = auto-detect")
    args = ap.parse_args()

    video = args.video.expanduser().resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = video.stem

    dur = probe_duration(video)
    print(f"video: {video.name}  duration: {dur/60:.1f} min", flush=True)

    import mlx_whisper  # noqa: E402  (import after arg check)
    import resource  # noqa: E402

    # [RAM guard] MLX's Metal buffer cache can grow unbounded across a long
    # transcription (observed ~11GB peak RSS on a 99-min run). Cap the cache
    # and the wired limit so peak RAM stays predictable on 16GB machines.
    try:
        import mlx.core as mx
        cache_limit = int(os.getenv("MLX_CACHE_LIMIT_GB", "2")) * 1024**3
        if hasattr(mx, "set_cache_limit"):
            mx.set_cache_limit(cache_limit)
        if hasattr(mx, "set_wired_limit"):
            mx.set_wired_limit(cache_limit)
    except Exception as e:  # noqa: BLE001
        print(f"note: could not set MLX memory limits ({e})", flush=True)

    # extract 16kHz mono audio
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        wav = Path(tf.name)
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
        check=True,
    )

    print(f"transcribing with {args.model} (word timestamps on)...", flush=True)
    result = mlx_whisper.transcribe(
        str(wav),
        path_or_hf_repo=args.model,
        word_timestamps=True,
        language=args.language,
        verbose=False,
    )
    wav.unlink(missing_ok=True)

    words_json = TRANSCRIPTS_DIR / f"{stem}.words.json"
    srt_path = TRANSCRIPTS_DIR / f"{stem}.srt"
    words_json.write_text(json.dumps(json_safe(result), ensure_ascii=False), encoding="utf-8")
    write_srt(result["segments"], srt_path)

    n_words = sum(len(s.get("words", [])) for s in result["segments"])
    peak_ram_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9
    print(f"language: {result.get('language')}", flush=True)
    print(f"segments: {len(result['segments'])}  words: {n_words}", flush=True)
    print(f"peak RAM: {peak_ram_gb:.2f} GB", flush=True)
    print(f"written: {srt_path}", flush=True)
    print(f"written: {words_json}", flush=True)


if __name__ == "__main__":
    main()
