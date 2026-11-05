"""``aclip transcript-fetch`` — the YouTube Track into the Project (ticket #25).

One track, fetched with the PyPI library from the project venv
(``youtube_transcript_api`` — spec decision 7), written into the Project's
transcript folder as plain JSON lines: ``{"text", "start", "duration"}``.
Language resolution follows ``settings.md`` (``language:``, default ``en``) —
never auto-detect. A video with no captions in the configured language is NOT
an interruption: the run goes whisper-only, and the fetch leaves an explicit
empty marker instead.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from aclip import settings as settings_module
from aclip.env import child_env, paths

# The library the app era called via yt-dlp's caption ladder now lives in the
# project venv (spec decision 7). Imported lazily: the aclip core stays
# stdlib-only, and the fetch re-execs under the venv python when the module is
# missing from the current interpreter (decision 4: venv python for sidecars).

TRANSCRIPT_DIRNAME = "transcript"
YOUTUBE_TRACK_FILENAME = "youtube_track.json"


class NoTracksError(RuntimeError):
    """The list endpoint itself failed (transcripts disabled, bad id, ...)."""


def _api_module() -> Any:
    """Import the library, re-execing under the venv python when absent."""
    try:
        import youtube_transcript_api  # noqa: PLC0415 — the lazy sidecar import

        return youtube_transcript_api
    except ImportError:
        venv_python = paths().venv_python
        if not venv_python.is_file():
            raise NoTracksError(
                f"{venv_python} is missing — the YouTube Track library lives in the project "
                "venv; run the autoclip-setup skill (or tools/setup.sh) to repair it"
            ) from None
        os.execve(
            str(venv_python),
            [str(venv_python), "-m", "aclip", "transcript-fetch", *sys.argv[2:]],
            child_env(),
        )
        raise AssertionError("unreachable")  # pragma: no cover


def _track_payload(track: Any) -> dict[str, Any]:
    """Normalise one API track object into a plain dict."""
    return {
        "language": str(getattr(track, "language", "")),
        "language_code": str(getattr(track, "language_code", "")),
        "is_generated": bool(getattr(track, "is_generated", False)),
        "is_translatable": bool(getattr(track, "is_translatable", False)),
    }


def list_tracks(video_id: str) -> list[dict[str, Any]]:
    """The video's caption tracks as plain dicts (library call — network)."""
    api_cls = getattr(_api_module(), "YouTubeTranscriptApi")
    tracks: list[dict[str, Any]] = []
    for track in api_cls().list(video_id):
        tracks.append(_track_payload(track))
    return tracks


def choose_track(
    tracks: Sequence[dict[str, Any]], language: str
) -> dict[str, Any] | None:
    """Pick the track for the configured language, deterministically.

    Exact code match first (``en``), then regional variants (``en-US`` —
    startswith ``<language>-``); among the matches a manually-created track
    beats an auto-generated one, and list order breaks the rest. No match → None
    (the caller writes the empty marker; the run goes whisper-only).
    """
    wanted = str(language).strip()
    exact = [t for t in tracks if t["language_code"] == wanted]
    pool = exact or [t for t in tracks if t["language_code"].startswith(wanted + "-")]
    if not pool:
        return None
    manual = [t for t in pool if not t["is_generated"]]
    return (manual or pool)[0]


def _snippet_field(snippet: Any, name: str) -> Any:
    """Read ``text``/``start``/``duration`` off a snippet (dict or object)."""
    if isinstance(snippet, dict):
        return snippet[name]
    return getattr(snippet, name)


def fetch_lines(video_id: str, track: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch the track's lines as ``{"text", "start", "duration"}`` dicts."""
    api_cls = getattr(_api_module(), "YouTubeTranscriptApi")
    listed = api_cls().list(video_id)
    chosen = None
    for candidate in listed:
        if _track_payload(candidate)["language_code"] == track["language_code"]:
            chosen = candidate
            break
    if chosen is None:
        raise NoTracksError(f"track {track['language_code']} vanished from the list")
    return [
        {
            "text": str(_snippet_field(snippet, "text")),
            "start": float(_snippet_field(snippet, "start")),
            "duration": float(_snippet_field(snippet, "duration")),
        }
        for snippet in chosen.fetch()
    ]


def youtube_track_path(project_dir: Path | str) -> Path:
    """``transcript/youtube_track.json`` — the fetched lines, or the empty marker."""
    return Path(project_dir) / TRANSCRIPT_DIRNAME / YOUTUBE_TRACK_FILENAME


def write_youtube_track(
    project_dir: Path | str, video_id: str, track: dict[str, Any], lines: Sequence[dict[str, Any]]
) -> Path:
    """Write the YouTube Track JSON into the Project's transcript folder."""
    path = youtube_track_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "video_id": video_id,
        "language": track["language"],
        "language_code": track["language_code"],
        "is_generated": track["is_generated"],
        "line_count": len(lines),
        "lines": list(lines),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_empty_marker(project_dir: Path | str, video_id: str, language: str) -> Path:
    """No captions in the configured language — the explicit empty marker.

    The run continues whisper-only (spec transcript policy); this is the one
    sanctioned silent path, and the end-of-run report reads the marker.
    """
    path = youtube_track_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "video_id": video_id,
        "language_code": language,
        "empty": True,
        "line_count": 0,
        "lines": [],
        "note": (
            f"no YouTube captions in '{language}' — the run continues whisper-only "
            "(the Whisper Track is the caption source; this marker is why)"
        ),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def project_dir_for(video_id: str) -> Path:
    """``projects/<Video Id>`` in the configured project root."""
    return paths().project_root / "projects" / video_id


def track_replayable(path: Path, language: str) -> bool:
    """A cached YouTube Track (or its empty marker) for ``language``.

    Replay is keyed to the configured language: a cached ``en`` track never
    answers a ``ms`` run (the user changed settings, so the track is fetched
    again), and an unreadable or half-written file is never replayed.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and str(data.get("language_code", "")).strip() == str(language).strip()
        and isinstance(data.get("lines"), list)
    )


def fetch_to_project(video_id: str, language: str | None = None) -> tuple[int, Path]:
    """Fetch (or mark) the YouTube Track for one Project. Returns (exit, path).

    A cached track for the configured language replays instead of hitting
    YouTube again — AGENTS.md: transcripts are never re-fetched, and the golden
    run's second pass must cost no network either.
    """
    resolved = language if language is not None else settings_module.load_settings().language
    project_dir = project_dir_for(video_id)
    cached = youtube_track_path(project_dir)
    if track_replayable(cached, resolved):
        return 0, cached
    tracks = list_tracks(video_id)
    track = choose_track(tracks, resolved)
    if track is None:
        return 0, write_empty_marker(project_dir, video_id, resolved)
    return 0, write_youtube_track(project_dir, video_id, track, fetch_lines(video_id, track))


def main(argv: Sequence[str] | None = None) -> int:
    """``aclip transcript-fetch <video id>`` — exit 0 on both real outcomes."""
    if argv is None:
        argv = sys.argv[2:]
    argv = list(argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: aclip transcript-fetch <Video Id>", file=sys.stderr)
        return 0 if argv and argv[0] in {"-h", "--help"} else 2
    video_id = argv[0]
    try:
        _, path = fetch_to_project(video_id)
    except NoTracksError as exc:
        print(f"transcript-fetch: {exc}", file=sys.stderr)
        return 1
    print(str(path))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    sys.exit(main())
