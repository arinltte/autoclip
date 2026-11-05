#!/usr/bin/env python3
"""Whisper model preflight — make sure the transcription model lives in the
project folder (download-on-miss, ticket #4).

mlx-whisper resolves its model through a plain huggingface_hub
``snapshot_download`` (``mlx_whisper/load_models.py`` calls
``snapshot_download(repo_id=…)`` whenever the path is not a local dir), which
consults in order: ``$HF_HUB_CACHE``, then ``$HF_HOME/hub``, then
``~/.cache/huggingface/hub``. ``tools/env.sh`` pins ``$HF_HUB_CACHE`` to
``<project>/models/hf-cache``, so "the model is in the project folder" means
"snapshot_download resolves it from that cache dir". This module mirrors that
check offline: **present** ⇔ ``snapshot_download(repo_id, cache_dir=<dir>,
local_files_only=True)`` returns a snapshot, i.e. ``refs/main`` points at a
``snapshots/<sha>/`` dir with real files (verified against huggingface_hub
1.33: a snapshot without refs, and refs→missing-snapshot, both read absent;
note hf reads refs/main WITHOUT stripping — a hand-written ref with a trailing
newline reads as absent, so build caches with ``printf``, not ``echo``).

Resolution order (``plan()``, pure, resolver injectable):

1. project cache  (``$HF_HUB_CACHE`` = ``<project>/models/hf-cache``) — warm
   means **zero network**, no notice;
2. global cache   (``~/.cache/huggingface/hub``, or ``$HF_HOME/hub`` when
   HF_HOME points outside the project folder — env.sh re-points HF_HOME into
   the project, which is not a global cache) — **copy** the
   ``models--<org>--<name>`` tree into the project cache, never re-download;
3. download from HuggingFace **into the project cache**.

Dual use:

* imported — the GUI calls :func:`ensure` in-process with progress callbacks
  (at app start and before the first transcription of a run);
* CLI — ``python tools/whisper_preflight.py --ensure <model>`` prints the line
  protocol the Rust sidecar (ticket #6) parses on stdout:

      progress <pct> <detail>     (pct: int 0–100)
      ok <snapshot-path>
      error <message>

Progress callbacks receive ``(done_bytes, total_bytes_or_None, detail)`` and
are advisory: a raising callback never fails the transfer.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import snapshot_download
from tqdm.auto import tqdm

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
COPY_CHUNK = 1024 * 1024          # 1 MiB — progress ticks once per chunk


@dataclass(frozen=True)
class Plan:
    """What :func:`plan` decided. ``none`` carries the warm ``path``; ``copy``
    carries the global ``src`` tree and the project ``dst``; ``download``
    carries the project cache as ``dst``."""

    action: str            # "none" | "copy" | "download"
    model: str
    path: Path | None = None
    src: Path | None = None
    dst: Path | None = None


def default_model() -> str:
    return os.environ.get("WHISPER_MODEL") or DEFAULT_MODEL


def project_cache_dir() -> Path:
    """The project-local cache — ``$HF_HUB_CACHE`` (env.sh pins it), else
    ``<project>/models/hf-cache``."""
    return Path(os.environ.get("HF_HUB_CACHE") or (PROJECT_DIR / "models" / "hf-cache"))


def global_cache_dir() -> Path:
    """The user-level HuggingFace cache — what a non-AutoClip HF install uses.

    ``$HF_HOME/hub`` when HF_HOME is set **outside** the project folder; env.sh
    re-points HF_HOME into the project (``$PROJECT/models/hf``), which is not a
    global cache, so in that case fall back to the hub default.
    """
    hf_home = os.environ.get("HF_HOME")
    if hf_home and not Path(hf_home).is_relative_to(PROJECT_DIR):
        return Path(hf_home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def resolve(model: str, cache_dir: Path | str) -> Path | None:
    """Offline presence check — exactly what mlx-whisper consults (see the
    module docstring). Returns the snapshot dir, or None when absent. Any
    resolution failure reads as "absent"."""
    try:
        return Path(snapshot_download(repo_id=model, cache_dir=str(cache_dir),
                                      local_files_only=True))
    except Exception:  # noqa: BLE001 — not-present is not an error here
        return None


def plan(model: str, project_cache: Path | str, global_cache: Path | str,
         resolve=resolve) -> Plan:
    """Decide none / copy / download, in that order (pure; resolve injectable)."""
    warm = resolve(model, project_cache)
    if warm is not None:
        return Plan("none", model, path=warm)
    elsewhere = resolve(model, global_cache)
    if elsewhere is not None:
        tree = elsewhere.parent.parent          # snapshots/<sha> → snapshots → models-- tree
        if tree.parent != Path(project_cache):  # a partial project tree is not a copy source
            return Plan("copy", model, src=tree, dst=Path(project_cache) / tree.name)
    return Plan("download", model, dst=Path(project_cache))


# --- executors (injectable for tests; tests never touch the network) ---------


def _human_size(n: float) -> str:
    """SI (1000-base) units — the same convention HuggingFace's repo page uses,
    so 'downloading 2.4 GB' matches what the model card promises."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"                        # unreachable, keeps type-checkers calm


def _emit(on_progress, done: int, total: int | None, detail: str) -> None:
    """Progress is advisory: a raising consumer never fails the transfer."""
    if on_progress is None:
        return
    try:
        on_progress(done, total, detail)
    except Exception:  # noqa: BLE001
        pass


def copy_model(src: Path, dst_cache: Path | str, on_progress=None) -> Path:
    """Copy a ``models--<org>--<name>`` tree into the project cache, preserving
    the HF layout (snapshot files are relative symlinks into ``blobs/``).

    Blobs go first and ``refs/main`` is written **last** — a copy interrupted
    mid-way leaves the refs absent, so :func:`resolve` still reads "absent" and
    the next ensure starts over instead of trusting a partial snapshot. A stale
    ``dst`` from an earlier partial copy is replaced.
    """
    src, dst = Path(src), Path(dst_cache) / Path(src).name
    if dst.exists():
        shutil.rmtree(dst)

    def order(rel: Path) -> tuple[int, str]:
        head = rel.parts[0] if rel.parts else ""
        rank = {"blobs": 0, "snapshots": 1}.get(head, 2)   # refs (and strays) last
        return rank, str(rel)

    files = sorted((p.relative_to(src) for p in src.rglob("*") if p.is_file() or p.is_symlink()),
                   key=order)
    total = sum(p.stat().st_size for p in src.rglob("*") if p.is_file() and not p.is_symlink())
    detail = f"copying {_human_size(total)}"
    done = 0
    last_done = -1
    for rel in files:
        s, d = src / rel, dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_symlink():
            os.symlink(os.readlink(s), d)       # relative link, same shape — still resolves
            continue
        with s.open("rb") as fsrc, d.open("wb") as fdst:
            while True:
                chunk = fsrc.read(COPY_CHUNK)
                if not chunk:
                    break
                fdst.write(chunk)
                done += len(chunk)
                last_done = done
                _emit(on_progress, done, total, detail)
    if done != last_done:                        # never double-report the final tick
        _emit(on_progress, done, total, detail)
    return dst


class _ProgressSink:
    """Throttles tqdm bar updates into ``(done, total, detail)`` callbacks —
    one call per integer-percent change, so neither the GUI log nor the CLI
    protocol is spammed."""

    def __init__(self, on_progress) -> None:
        self._cb = on_progress
        self._lock = threading.Lock()
        self._last: tuple[int, int | None] | None = None

    def feed(self, done: int, total: int | None) -> None:
        with self._lock:
            pct = int(done * 100 / total) if total else 0
            key = (pct, total)
            if key == self._last:
                return
            self._last = key
        detail = f"downloading {_human_size(total)}" if total else "downloading"
        _emit(self._cb, done, total, detail)


def _progress_tqdm_class(sink: _ProgressSink):
    """A tqdm subclass that feeds ``sink`` from huggingface_hub's aggregate
    **"Downloading bytes"** bar (created by ``snapshot_download`` when
    ``tqdm_class=`` is passed; its total converges to the whole snapshot's
    byte size). Bars with any other desc/unit are ignored. huggingface_hub
    passes a ``name=`` kwarg its own subclass pops — vanilla tqdm rejects it,
    so pop it here too."""

    class _ProgressTqdm(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs.pop("name", None)
            self._track = kwargs.get("desc") == "Downloading bytes"
            self._count = int(kwargs.get("initial") or 0)
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            # Count bytes here, not via self.n: a disabled bar (no TTY,
            # HF_HUB_DISABLE_PROGRESS_BARS) never advances tqdm's counter, and
            # the hook must keep reporting progress anyway.
            if self._track and n:
                self._count += int(n)
                total = int(self.total) if self.total else None
                sink.feed(self._count, total)
            super().update(n)

    return _ProgressTqdm


def download_model(model: str, cache_dir: Path | str, on_progress=None) -> Path:
    """Download the model snapshot into ``cache_dir`` (the project cache) with
    aggregate byte progress. Raw tqdm bars stay on stderr — stdout carries only
    the line protocol."""
    sink = _ProgressSink(on_progress)
    return Path(snapshot_download(repo_id=model, cache_dir=str(cache_dir),
                                  tqdm_class=_progress_tqdm_class(sink)))


# --- ensure(): plan → execute → verify ----------------------------------------


def ensure(model: str | None = None, *, project_cache=None, global_cache=None,
           on_plan=None, on_progress=None, resolve=resolve,
           download=download_model, copy=copy_model) -> Path:
    """Make ``model`` resolvable from the project cache; return the snapshot dir.

    Plans first (offline); only a miss acts. ``on_plan(Plan)`` fires before the
    action runs; ``on_progress`` streams bytes while it runs. Raises when the
    model still does not resolve afterwards — the caller decides whether that
    is fatal (transcription) or a logged warning (app start).
    """
    model = model or default_model()
    project_cache = Path(project_cache) if project_cache else project_cache_dir()
    global_cache = Path(global_cache) if global_cache else global_cache_dir()
    p = plan(model, project_cache, global_cache, resolve=resolve)
    if on_plan is not None:
        try:
            on_plan(p)
        except Exception:  # noqa: BLE001 — advisory
            pass
    if p.action == "none":
        return p.path
    if p.action == "copy":
        copy(p.src, project_cache, on_progress=on_progress)
    else:
        download(model, project_cache, on_progress=on_progress)
    done = resolve(model, project_cache)
    if done is None:
        raise RuntimeError(
            f"model {model} did not resolve from the project cache after {p.action}")
    return done


# --- CLI: the line protocol the Rust sidecar (#6) parses -----------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ensure", nargs="?", const=None, default=None, metavar="MODEL",
                    help="ensure the model is in the project cache "
                         "(default: $WHISPER_MODEL, then the builtin default)")
    args = ap.parse_args(argv)
    model = args.ensure or default_model()

    def _on_progress(done, total, detail):
        pct = int(done * 100 / total) if total else 0
        print(f"progress {pct} {detail}", flush=True)

    try:
        path = ensure(model, on_progress=_on_progress)
    except Exception as e:  # noqa: BLE001 — the protocol carries the failure
        print(f"error {e}", flush=True)
        return 1
    print(f"ok {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
