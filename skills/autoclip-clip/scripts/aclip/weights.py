"""Whisper weights preflight — the model resolves from the PROJECT folder
(ticket #27; ported from the app era's ``tools/whisper_preflight.py``).

mlx-whisper resolves its model via a plain ``snapshot_download`` that consults
``$HF_HUB_CACHE`` first; the env contract pins that to
``<project>/models/hf-cache``, so "the weights are in the project folder" means
"snapshot_download resolves them from that cache dir". This module makes that
true, offline-first:

1. project cache present  → warm: zero network, no action;
2. global cache present   → copy the ``models--<org>--<name>`` tree in
   (never re-download);
3. miss                   → download into the project cache.

``plan`` is pure and resolver-injectable (tests never touch the network).
``copy_model`` preserves the HF layout (blobs first, ``refs/main`` written
last — an interrupted copy leaves the refs absent, so ``resolve`` still reads
"absent" and the next ensure starts over instead of trusting a partial
snapshot). ``ensure`` plans, executes, then verifies — the caller decides
whether an unresolved model is fatal.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from aclip.env import child_env, paths

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
COPY_CHUNK = 1024 * 1024  # 1 MiB — progress ticks once per chunk


@dataclass(frozen=True)
class Plan:
    """What :func:`plan` decided. ``none`` carries the warm ``path``; ``copy``
    carries the global ``src`` tree and the project ``dst``; ``download``
    carries the project cache as ``dst``."""

    action: str  # "none" | "copy" | "download"
    model: str
    path: Path | None = None
    src: Path | None = None
    dst: Path | None = None


def default_model() -> str:
    """The whisper model repo: ``$WHISPER_MODEL``, else the frozen default."""
    return os.environ.get("WHISPER_MODEL") or DEFAULT_MODEL


def project_cache_dir() -> Path:
    """The project-local cache — ``$HF_HUB_CACHE`` (the env contract pins it)."""
    override = os.environ.get("HF_HUB_CACHE")
    return Path(override) if override else paths().hf_cache


def global_cache_dir() -> Path:
    """The user-level HuggingFace cache — what a non-AutoClip HF install uses.

    ``$HF_HOME/hub`` when HF_HOME is set outside the project folder; the env
    contract re-points HF_HOME into the project, which is not a global cache.
    """
    hf_home = os.environ.get("HF_HOME")
    root = paths().project_root
    if hf_home and not Path(hf_home).resolve().is_relative_to(root):
        return Path(hf_home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _snapshot_download(*, repo_id: str, cache_dir: str, local_files_only: bool, tqdm_class: Any = None) -> str:
    """Lazy import: huggingface_hub lives in the project venv."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        venv_python = paths().venv_python
        if venv_python.is_file():
            os.execve(
                str(venv_python),
                [str(venv_python), "-m", "aclip", *sys.argv[2:]],
                child_env(),
            )
        raise RuntimeError(
            f"{venv_python} is missing — the whisper weights tooling lives in the project "
            "venv; run the autoclip-setup skill (or tools/setup.sh) to repair it"
        ) from None
    return snapshot_download(repo_id=repo_id, cache_dir=cache_dir, local_files_only=local_files_only, tqdm_class=tqdm_class)


def resolve(model: str, cache_dir: Path | str) -> Path | None:
    """Offline presence check — exactly what mlx-whisper consults. Returns the
    snapshot dir, or None when absent. Any resolution failure reads as
    "absent"."""
    try:
        return Path(
            _snapshot_download(repo_id=model, cache_dir=str(cache_dir), local_files_only=True)
        )
    except Exception:  # noqa: BLE001 — not-present is not an error here
        return None


def plan(
    model: str,
    project_cache: Path | str,
    global_cache: Path | str,
    resolve: Callable[[str, Path | str], Path | None] = resolve,
) -> Plan:
    """Decide none / copy / download, in that order (pure; resolve injectable)."""
    warm = resolve(model, project_cache)
    if warm is not None:
        return Plan("none", model, path=warm)
    elsewhere = resolve(model, global_cache)
    if elsewhere is not None:
        tree = elsewhere.parent.parent  # snapshots/<sha> → snapshots → models-- tree
        if tree.parent != Path(project_cache):  # a partial project tree is not a copy source
            return Plan("copy", model, src=tree, dst=Path(project_cache) / tree.name)
    return Plan("download", model, dst=Path(project_cache))


def _human_size(n: float) -> str:
    """SI (1000-base) units — the convention HuggingFace's repo page uses."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def _emit(on_progress: Any, done: int, total: int | None, detail: str) -> None:
    """Progress is advisory: a raising consumer never fails the transfer."""
    if on_progress is None:
        return
    try:
        on_progress(done, total, detail)
    except Exception:  # noqa: BLE001
        pass


def copy_model(src: Path, dst_cache: Path | str, on_progress: Any = None) -> Path:
    """Copy a ``models--<org>--<name>`` tree into the project cache, preserving
    the HF layout (snapshot files are relative symlinks into ``blobs/``).

    Blobs go first (relative symlinks preserved) and ``refs/main`` is written
    last; a stale ``dst`` from an earlier partial copy is replaced.
    """
    src, dst = Path(src), Path(dst_cache) / Path(src).name
    if dst.exists():
        shutil.rmtree(dst)

    def order(rel: Path) -> tuple[int, str]:
        head = rel.parts[0] if rel.parts else ""
        rank = {"blobs": 0, "snapshots": 1}.get(head, 2)  # refs (and strays) last
        return rank, str(rel)

    files = sorted(
        (p.relative_to(src) for p in src.rglob("*") if p.is_file() or p.is_symlink()),
        key=order,
    )
    total = sum(p.stat().st_size for p in src.rglob("*") if p.is_file() and not p.is_symlink())
    detail = f"copying {_human_size(total)}"
    done = 0
    last_done = -1
    for rel in files:
        s, d = src / rel, dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_symlink():
            os.symlink(os.readlink(s), d)
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
    if done != last_done:  # never double-report the final tick
        _emit(on_progress, done, total, detail)
    return dst


def download_model(model: str, cache_dir: Path | str, on_progress: Any = None) -> Path:
    """Download the model snapshot into ``cache_dir`` (the project cache)."""
    return Path(_snapshot_download(repo_id=model, cache_dir=str(cache_dir), local_files_only=False))


def ensure(
    model: str | None = None,
    *,
    project_cache: Path | str | None = None,
    global_cache: Path | str | None = None,
    on_plan: Any = None,
    on_progress: Any = None,
    resolve: Callable[[str, Path | str], Path | None] = resolve,
    download: Any = download_model,
    copy: Any = copy_model,
) -> Path:
    """Make ``model`` resolvable from the project cache; return the snapshot dir.

    Plans first (offline); only a miss acts. Raises when the model still does
    not resolve afterwards.
    """
    model = model or default_model()
    project_cache = Path(project_cache) if project_cache else project_cache_dir()
    global_cache = Path(global_cache) if global_cache else global_cache_dir()
    planned = plan(model, project_cache, global_cache, resolve=resolve)
    if on_plan is not None:
        try:
            on_plan(planned)
        except Exception:  # noqa: BLE001 — advisory
            pass
    if planned.action == "none":
        return planned.path  # type: ignore[return-value]
    if planned.action == "copy":
        copy(planned.src, project_cache, on_progress=on_progress)
    else:
        download(model, project_cache, on_progress=on_progress)
    done = resolve(model, project_cache)
    if done is None:
        raise RuntimeError(
            f"model {model} did not resolve from the project cache after {planned.action}"
        )
    return done


def main(argv: Sequence[str] | None = None) -> int:
    """Standalone preflight entry — ``python -m aclip.weights [MODEL]``."""
    argv = list(sys.argv[2:] if argv is None else argv)
    model = argv[0] if argv else default_model()

    def _on_progress(done: int, total: int | None, detail: str) -> None:
        pct = int(done * 100 / total) if total else 0
        print(f"progress {pct} {detail}", flush=True)

    def _on_plan(action: str, model: str, _detail: str) -> None:
        if action != "none":
            print(f"{action} {model}", flush=True)

    def _plan_cb(planned: Any) -> None:
        _on_plan(planned.action, planned.model, "")

    try:
        path = ensure(model, on_plan=_plan_cb, on_progress=_on_progress)
    except Exception as exc:  # noqa: BLE001 — the caller decides fatality
        print(f"error {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"ok {path}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    sys.exit(main())
