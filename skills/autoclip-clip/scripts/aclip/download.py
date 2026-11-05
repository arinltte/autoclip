"""``aclip download`` — the three-rung quality ladder (ticket #28).

Ported from the app era's ``src/download.rs`` (itself the port of
``tools/download_plan.py`` + gui.py's download stage): the VENV yt-dlp (the
bgutil PO-token provider plugin is vendored at ``tools/vendor/`` — the system
yt-dlp lacks it and 403s on the higher formats), the ``--extractor-args`` PO
token, and the fixed three-rung ladder — no quality knob:

1. best ≤1080p mp4/m4a →
2. H.264-preferring ≤720p →
3. ≤720p again, after dropping the cached PO token.

An existing media file is the resume case: re-running costs nothing. All three
rungs exhausted is a hard failure carrying the exact last error. The plan half
is pure (ladder, destinations, PO-token cache, partials, percent parsing, error
extraction); the sidecar run goes through an injectable ``runner`` so tests
never spawn yt-dlp.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from aclip.env import child_env, paths
from aclip.scaffold import scaffold_project

# --- the retry ladder (download_plan.py) ----------------------------------------

# Attempt 1 — best ≤1080p mp4/m4a.
FORMAT_BEST_1080 = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b"
# Attempts 2–3 — H.264-preferring, 720p-capped.
FORMAT_AVC_720 = "bv*[ext=mp4][vcodec^=avc1][height<=720]+ba[ext=m4a]/b[ext=mp4][vcodec^=avc1][height<=720]/bv*[height<=720]+ba/b[height<=720]"


@dataclass(frozen=True)
class Attempt:
    """One rung of the ladder: what to run and what the log says."""

    n: int
    fmt: str
    clear_token_cache: bool  # only the last rung discards the cached PO token
    label: str


def build_ladder() -> list[Attempt]:
    """The three attempts, in order."""
    return [
        Attempt(1, FORMAT_BEST_1080, False, "download attempt 1/3 — best available quality"),
        Attempt(2, FORMAT_AVC_720, False, "download attempt 2/3 — safer H.264 formats (≤720p)"),
        Attempt(3, FORMAT_AVC_720, True, "download attempt 3/3 — fresh PO token, safer formats"),
    ]


# --- destinations + the "downloaded" predicate ------------------------------------


def download_path(download_dir: Path | str, video_id: str) -> Path:
    """Destination and naming exactly as the app era: ``<video-id>_hi.mp4``."""
    return Path(download_dir) / f"{video_id}_hi.mp4"


def downloaded_ok(path: Path | str) -> bool:
    """A finished download exists and is non-empty; ``.part`` files never count."""
    p = Path(path)
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


# --- the bgutil PO-token provider cache (download_plan.py) -----------------------


def token_cache_dir_from(xdg: str | None, home: Path | str) -> Path:
    """An explicit dir wins, else ``$XDG_CACHE_HOME``, else ``~/.cache``."""
    if xdg:
        return Path(xdg) / "bgutil-ytdlp-pot-provider"
    return Path(home) / ".cache" / "bgutil-ytdlp-pot-provider"


def token_cache_dir() -> Path:
    """The live cache dir (the pure core reads the env live)."""
    home = os.environ.get("HOME") or str(Path.home())
    return token_cache_dir_from(os.environ.get("XDG_CACHE_HOME"), home)


def clear_token_cache_entry_at(cache_dir: Path | str, video_id: str) -> bool:
    """Drop one video's entry from the provider's ``cache.json``.

    A missing/unreadable cache or an absent entry is a no-op returning false;
    every other video's entry is left intact.
    """
    cache = Path(cache_dir) / "cache.json"
    try:
        data = cache.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        payload = json.loads(data)
    except ValueError:
        return False
    if not isinstance(payload, dict) or payload.pop(video_id, None) is None:
        return False
    try:
        cache.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return False
    return True


# --- stale partials (download_plan.py) -------------------------------------------


def stale_partials(download_path: Path | str) -> list[Path]:
    """Left-behind temporaries of an interrupted download (never real output):
    siblings of the destination starting with ``<stem>.`` that end in
    ``.part``/``.ytdl`` or carry a yt-dlp fragment marker (``.f`` in the name —
    the same overreach as the Python)."""
    path = Path(download_path)
    parent = path.parent
    stem = path.stem
    if not parent.is_dir() or not stem:
        return []
    prefix = f"{stem}."
    found = [
        p
        for p in sorted(parent.iterdir())
        if p.name.startswith(prefix)
        and (p.name.endswith(".part") or ".f" in p.name or p.name.endswith(".ytdl"))
    ]
    return found


def clean_partials(download_path: Path | str) -> int:
    """Delete leftover partials so a retry starts clean; returns how many."""
    removed = 0
    for partial in stale_partials(download_path):
        try:
            partial.unlink()
            removed += 1
        except OSError:
            continue
    return removed


# --- progress parsing + error extraction ------------------------------------------


def parse_download_percent(line: str) -> tuple[float, str] | None:
    """``[download]  45.6% of ...`` → (45.6, "downloading 45.6%") — the exact
    search(r"\\[download\\]\\s+(\\d+(?:\\.\\d+)?)%", line) semantics."""
    match = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
    if match:
        pct = float(match.group(1))
        return pct, f"downloading {pct}%"
    return None


def last_log_error(lines: Sequence[str]) -> str | None:
    """The exact last error line: the tail after ``ERROR:`` on the newest line
    containing "HTTP Error" or "unable to download"."""
    for line in reversed(list(lines)):
        if "HTTP Error" in line or "unable to download" in line:
            _, separator, tail = line.rpartition("ERROR:")
            return (tail.strip() if separator else line.strip()) or None
    return None


# --- source → video id -------------------------------------------------------------


def extract_video_id(source: str) -> str | None:
    """The Video Id from a YouTube URL (watch?v=, youtu.be/, /shorts|live|embed/…)
    or a local file's stem — the app era's probe, made deterministic."""
    if re.match(r"^https?://", source):
        patterns = (
            r"[?&]v=([A-Za-z0-9_-]{11})",
            r"youtu\.be/([A-Za-z0-9_-]{11})",
            r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})",
        )
        for pattern in patterns:
            found = re.search(pattern, source)
            if found:
                return found.group(1)
        return None
    stem = Path(source).stem
    return stem or None


# --- the exact yt-dlp invocation ----------------------------------------------------


def pot_extractor_arg(tools_dir: Path | str) -> str:
    """The ``--extractor-args`` pointing yt-dlp at the vendored bgutil server."""
    server = Path(tools_dir) / "vendor" / "bgutil-ytdlp-pot-provider" / "server"
    return f"youtubepot-bgutilscript:server_home={server}"


def download_args(source: str, dest: Path, fmt: str, pot: str) -> list[str]:
    """The download command's args, exactly as the app era built them per rung."""
    return [
        "--no-update",
        "--extractor-args",
        pot,
        "-f",
        fmt,
        "--merge-output-format",
        "mp4",
        "--newline",
        "-o",
        str(dest),
        source,
    ]


# --- the stage body --------------------------------------------------------------------


class DownloadError(RuntimeError):
    """All rungs exhausted; ``last_error`` carries the exact last error line."""

    def __init__(self, message: str, last_error: str | None = None) -> None:
        super().__init__(message)
        self.last_error = last_error


# runner(argv, env) -> CompletedProcess-like (returncode, stdout); injectable.
Runner = Callable[[list[str], dict[str, str]], Any]


def default_runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """The real runner: the venv yt-dlp (``-m yt_dlp`` under the venv python —
    it carries the vendored bgutil plugin)."""
    python = paths().venv_python
    full = [str(python), "-m", "yt_dlp", *argv]
    return subprocess.run(  # noqa: S603 — the fixed argv; shell=False
        full, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False
    )


def run_download(
    video_id: str,
    source: str,
    runner: Runner | None = None,
    on_progress: Callable[[float, str], None] | None = None,
    clear_token_cache: Callable[[str], bool] = lambda video_id: False,
    tools_dir: Path | str | None = None,
) -> Path:
    """Download the media through the three-rung ladder; return its path.

    An existing destination is the resume case — no rung runs. Otherwise the
    rungs run in order: stale partials cleaned before each, the cached PO token
    dropped on the last rung, success stops the ladder, exhaustion raises
    ``DownloadError`` with the exact last error.
    """
    runner = runner or default_runner
    resolved_tools = Path(tools_dir) if tools_dir else paths().project_root / "tools"
    scaffold_project(video_id)
    dest = download_path(paths().project_root / "projects" / video_id / "download", video_id)
    if downloaded_ok(dest):
        if on_progress is not None:
            on_progress(100.0, "already downloaded — skipped")
        return dest

    pot = pot_extractor_arg(resolved_tools)
    all_lines: list[str] = []
    last_error: str | None = None
    for attempt in build_ladder():
        clean_partials(dest)
        if attempt.clear_token_cache:
            clear_token_cache(video_id)
        lines: list[str] = []
        proc = runner(download_args(source, dest, attempt.fmt, pot), child_env())
        stdout = getattr(proc, "stdout", "") or ""
        lines = [line for line in stdout.splitlines() if line is not None]
        for line in lines:
            parsed = parse_download_percent(line)
            if parsed and on_progress is not None:
                on_progress(*parsed)
        all_lines.extend(lines)
        if getattr(proc, "returncode", 0) == 0 and downloaded_ok(dest):
            return dest
        found = last_log_error(lines) or last_log_error(all_lines)
        if found:
            last_error = found
    detail = f" — exact last error: {last_error}" if last_error else " — no error detail captured"
    raise DownloadError(
        f"download exhausted all 3 rungs for {video_id}{detail}; "
        "the ladder is fixed behaviour — the clip skill treats this as a hard failure",
        last_error=last_error,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """``aclip download <source> [--video-id ID]`` — exit 0 or a hard failure."""
    argv = list(sys.argv[2:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: aclip download <Video URL or local file> [--video-id ID]", file=sys.stderr)
        return 0 if argv and argv[0] in {"-h", "--help"} else 2
    source = argv[0]
    video_id: str | None = None
    rest = argv[1:]
    index = 0
    while index < len(rest):
        if rest[index] == "--video-id" and index + 1 < len(rest):
            video_id = rest[index + 1]
            index += 2
        else:
            index += 1
    video_id = video_id or extract_video_id(source)
    if not video_id:
        print(f"download: could not read a Video Id from {source!r}", file=sys.stderr)
        return 2
    try:
        path = run_download(video_id, source, on_progress=lambda pct, detail: print(f"{detail} ({pct:.1f}%)"))
    except DownloadError as exc:
        print(f"download: {exc}", file=sys.stderr)
        return 1
    print(str(path))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    sys.exit(main())
