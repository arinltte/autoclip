"""``aclip transcribe`` — the Whisper Track into the Project (ticket #27).

Runs mlx-whisper ``large-v3-turbo`` with word timestamps, with the weights
guaranteed to resolve from the PROJECT cache (``aclip.weights.ensure``:
warm = zero network, global-cache hit = copy in, miss = download into the
project cache), and writes into the Project's transcript folder:

* ``<stem>.words.json`` — the full whisper result, non-finite floats filtered
  (the NaN-avg-logprob regression stays pinned — mlx-whisper reports NaN on
  no-speech segments and strict JSON readers reject the NaN literal);
* ``<stem>.cues.json`` — the caption-sized cues the karaoke renderer drives,
  byte-exact with the app era's frozen renders;
* ``<stem>.srt`` — the clean cue SRT (it replaces the fragmented segment SRT).

``--language`` defaults from settings.md (``en`` — never auto; whisper
misdetected an English podcast as Hindi). The model run needs the project
venv, so the module re-execs under the venv python when mlx-whisper is
absent from the current interpreter (decision 4: venv python for sidecars).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from aclip import cues as cues_module
from aclip import settings as settings_module
from aclip import weights
from aclip.cues import non_finite_to_none
from aclip.env import child_env, paths

DEFAULT_MODEL = weights.DEFAULT_MODEL
SAMPLE_RATE = 16000


def _reexec_under_venv() -> None:
    venv_python = paths().venv_python
    if venv_python.is_file():
        os.execve(
            str(venv_python),
            [str(venv_python), "-m", "aclip", "transcribe", *sys.argv[2:]],
            child_env(),
        )
    raise RuntimeError(
        f"{venv_python} is missing — mlx-whisper lives in the project venv; run the "
        "autoclip-setup skill (or tools/setup.sh) to repair it"
    )


def _mlx_whisper() -> Any:
    try:
        import mlx_whisper  # noqa: PLC0415 — the lazy sidecar import

        return mlx_whisper
    except ImportError:
        _reexec_under_venv()  # replaces the process — never returns


def _set_mlx_memory_limits() -> None:
    """The RAM guard: MLX's Metal buffer cache can grow unbounded across a long
    transcription (~11 GB peak observed on a 99-min run). Cap the cache and the
    wired limit so peak RAM stays predictable on 16GB machines."""
    try:
        import mlx.core as mx  # noqa: PLC0415

        cache_limit = int(os.getenv("MLX_CACHE_LIMIT_GB", "2")) * 1024**3
        if hasattr(mx, "set_cache_limit"):
            mx.set_cache_limit(cache_limit)
        if hasattr(mx, "set_wired_limit"):
            mx.set_wired_limit(cache_limit)
    except Exception as exc:  # noqa: BLE001 — advisory only
        print(f"note: could not set MLX memory limits ({exc})", flush=True)


def find_media(project_dir: Path) -> Path:
    """The downloaded media in the Project's download folder."""
    download_dir = Path(project_dir) / "download"
    candidates = sorted(
        p for p in download_dir.glob("*") if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3"}
    )
    if not candidates:
        raise FileNotFoundError(f"no media file in {download_dir} — run aclip download first")
    return candidates[0]


def extract_audio(media: Path, ffmpeg_bin: Path) -> Path:
    """16kHz mono WAV for whisper (the model's expected input).

    The sidecar runs with the resolved contract env (decision 4: one env for
    every subprocess aclip launches), so the static ffmpeg and its PATH bits
    resolve the same way as from the CLI.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    wav = Path(tmp.name)
    subprocess.run(
        [str(ffmpeg_bin), "-v", "error", "-y", "-i", str(media), "-vn", "-ac", "1",
         "-ar", str(SAMPLE_RATE), str(wav)],
        check=True, env=child_env(),
    )
    return wav


def transcribe_media(media: Path, model: str, language: str) -> dict[str, Any]:
    """Run mlx-whisper with word timestamps; returns the raw result dict."""
    whisper = _mlx_whisper()
    _set_mlx_memory_limits()
    wav = extract_audio(media, paths().ffmpeg_bin)
    try:
        return whisper.transcribe(
            str(wav),
            path_or_hf_repo=model,
            word_timestamps=True,
            language=language,
            verbose=False,
        )
    finally:
        Path(wav).unlink(missing_ok=True)


def ensure_speech(result: dict[str, Any]) -> int:
    """Zero-speech media is a hard failure (spec decision 11): the run must not
    carry a wordless track through the pipeline. Returns the word count."""
    words = sum(len(segment.get("words", [])) for segment in result.get("segments", []))
    if words == 0:
        raise ValueError(
            "zero-speech media — the transcription produced no words "
            "(a music-only or silent video has no clips; this is a hard failure)"
        )
    return words


def whisper_output_paths(project_dir: Path, video_id: str) -> dict[str, Path]:
    """The three Whisper Track artifacts in the Project's transcript folder."""
    transcript_dir = Path(project_dir) / "transcript"
    return {
        "words": transcript_dir / f"{video_id}_hi.words.json",
        "cues": transcript_dir / f"{video_id}_hi.cues.json",
        "srt": transcript_dir / f"{video_id}_hi.srt",
    }


def _existing_media(project_dir: Path) -> str | None:
    """The Project's media path when one exists (informational in a replay)."""
    try:
        return str(find_media(project_dir))
    except FileNotFoundError:
        return None


def transcribed_ok(project_dir: Path, video_id: str) -> bool:
    """A finished Whisper Track exists — all three artifacts non-empty."""
    try:
        return all(
            path.is_file() and path.stat().st_size > 0
            for path in whisper_output_paths(project_dir, video_id).values()
        )
    except OSError:
        return False


def write_outputs(result: dict[str, Any], project_dir: Path, video_id: str) -> dict[str, Path]:
    """Words JSON (NaN-filtered), cue JSON and cue SRT into the transcript folder."""
    transcript_dir = Path(project_dir) / "transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    rebuilt = cues_module.rebuild_cues(result)
    paths_out = whisper_output_paths(project_dir, video_id)
    paths_out["words"].write_text(
        json.dumps(non_finite_to_none(result), ensure_ascii=False), encoding="utf-8"
    )
    paths_out["cues"].write_text(cues_module.render_cues_json(rebuilt), encoding="utf-8")
    paths_out["srt"].write_text(cues_module.render_cues_srt(rebuilt), encoding="utf-8")
    return paths_out


def transcribe_project(
    video_id: str,
    model: str | None = None,
    language: str | None = None,
    on_preflight: Any = None,
) -> dict[str, Any]:
    """The whole preflight + transcribe + write path for one Project.

    A finished Whisper Track replays from the Project rather than re-running
    mlx-whisper (AGENTS.md: transcripts are never re-fetched; the golden run's
    second pass must cost nothing) — no model run, no weights preflight, no
    media read. An explicit ``--model``/``--language`` is the one override that
    bypasses the replay: the caller asked for a different track, so one is
    produced.
    """
    if on_preflight is None:
        on_preflight = lambda planned: None  # noqa: E731
    project_dir = paths().project_root / "projects" / video_id
    if transcribed_ok(project_dir, video_id) and model is None and language is None:
        existing = whisper_output_paths(project_dir, video_id)
        words_result = json.loads(existing["words"].read_text(encoding="utf-8"))
        segments = words_result.get("segments") or []
        return {
            **existing,  # words/cues/srt as paths — the same keys a fresh run returns
            "language": words_result.get("language"),
            "model": settings_module.load_settings().model or DEFAULT_MODEL,
            "segments": len(segments),
            "word_count": sum(len(s.get("words") or []) for s in segments),
            "media": _existing_media(project_dir),
            "reused": True,
        }
    resolved = language if language is not None else settings_module.load_settings().language
    model = model or settings_module.load_settings().model or DEFAULT_MODEL
    # Preflight: the weights must resolve from the PROJECT cache (ticket #27).
    weights.ensure(model, on_plan=on_preflight)
    media = find_media(project_dir)
    result = transcribe_media(media, model, resolved)
    n_words = ensure_speech(result)  # hard failure before anything is written
    written = write_outputs(result, project_dir, video_id)
    return {
        "language": result.get("language"),
        "model": model,
        "segments": len(result.get("segments", [])),
        "words": n_words,
        "media": str(media),
        **written,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """``aclip transcribe <Video Id>`` — exit 0 on success, 1 on a real defect."""
    argv = list(sys.argv[2:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: aclip transcribe <Video Id> [--model REPO] [--language LANG]", file=sys.stderr)
        return 0 if argv and argv[0] in {"-h", "--help"} else 2
    video_id = argv[0]
    model: str | None = None
    language: str | None = None
    rest = argv[1:]
    index = 0
    while index < len(rest):
        if rest[index] == "--model" and index + 1 < len(rest):
            model = rest[index + 1]
            index += 2
        elif rest[index] == "--language" and index + 1 < len(rest):
            language = rest[index + 1]
            index += 2
        else:
            index += 1
    try:
        def _on_plan(planned: Any) -> None:
            if planned.action != "none":
                print(f"{planned.action} {planned.model} → project cache", flush=True)

        summary = transcribe_project(
            video_id,
            model=model,
            language=language,
            on_preflight=_on_plan,
        )
    except Exception as exc:  # noqa: BLE001 — a real defect, reported loudly
        print(f"transcribe: {exc}", file=sys.stderr)
        return 1
    if summary.get("reused"):
        print("already transcribed — skipped (the Whisper Track replays from the Project)")
    for key in ("words", "cues", "srt"):
        print(f"{'reused' if summary.get('reused') else 'written'}: {summary[key]}")
    print(f"language: {summary['language']}  segments: {summary['segments']}  words: {summary['words']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    sys.exit(main())
