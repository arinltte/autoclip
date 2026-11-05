"""The single environment contract for every aclip subcommand and sidecar.

This module replaces the two places the app era used to keep environment
setup: ``tools/env.sh`` and the sidecar env block on the Rust side. Both
collapse here so a subprocess launched by ``aclip`` inherits exactly one
resolved set of paths — no command rebuilds it.

Resolution is override-then-default and independent of the current working
directory: an exported environment variable wins, otherwise the path is
derived from :data:`PROJECT_ROOT`, which is itself either the exported
``AUTOCLIP_PROJECT_DIR`` or discovered by walking up from this file to the
directory that contains ``skills/``. That walk keeps the contract valid in a
clone checked out anywhere on disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Env var name -> Paths attribute for the contract keys every subprocess receives.
CONTRACT_KEYS = (
    "PROJECT_ROOT",
    "HF_HUB_CACHE",
    "FFMPEG_BIN",
    "VENV_PYTHON",
    "POT_PROVIDER_DIR",
    "SECRETS_DIR",
)

# Model identity for the whisper weights the doctor resolves from the cache.
WHISPER_MODEL_DIR_NAME = "models--mlx-community--whisper-large-v3-turbo"

SECRETS_API_KEY_FILENAME = "nvidia_api_key"


def _looks_like_project_root(candidate: Path) -> bool:
    """True when ``candidate`` is a checkout root (it holds ``skills/``)."""
    return (candidate / "skills").is_dir()


def _discover_project_root() -> Path:
    """Return the project root without depending on the cwd.

    ``AUTOCLIP_PROJECT_DIR`` wins when set; otherwise walk up from this file
    until a directory containing ``skills/`` is found. This file lives at
    ``<root>/skills/autoclip-clip/scripts/aclip/env.py``, so the walk reaches
    the root three levels up in any clone.
    """
    override = os.environ.get("AUTOCLIP_PROJECT_DIR")
    if override:
        return Path(override).expanduser().resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if _looks_like_project_root(parent):
            return parent

    # Fall back to the shipped layout rather than failing: this CLI lives
    # inside the project it drives.
    return here.parents[3]


PROJECT_ROOT: Path = _discover_project_root()


@dataclass(frozen=True)
class Paths:
    """Resolved filesystem locations of the aclip contract."""

    project_root: Path
    hf_cache: Path
    ffmpeg_bin: Path
    venv_python: Path
    pot_provider_dir: Path
    secrets_dir: Path
    secrets_api_key: Path


def paths() -> Paths:
    """Resolve the contract paths, override-then-default.

    Every value is re-read from ``os.environ`` on each call so a test or a
    long-running process sees the current environment instead of an import
    time snapshot.
    """
    root = _discover_project_root()

    def resolve(name: str, default: Path) -> Path:
        value = os.environ.get(name)
        return Path(value).expanduser() if value else default

    hf_cache = resolve("HF_HUB_CACHE", root / "models" / "hf-cache")
    ffmpeg_bin = resolve("FFMPEG_BIN", root / "tools" / "bin" / "ffmpeg")
    venv_python = resolve("VENV_PYTHON", root / ".venv" / "bin" / "python")
    pot_provider_dir = resolve("POT_PROVIDER_DIR", root / "tools" / "vendor" / "bgutil-ytdlp-pot-provider")
    secrets_dir = resolve("SECRETS_DIR", root / ".secrets")

    return Paths(
        project_root=root,
        hf_cache=hf_cache,
        ffmpeg_bin=ffmpeg_bin,
        venv_python=venv_python,
        pot_provider_dir=pot_provider_dir,
        secrets_dir=secrets_dir,
        secrets_api_key=secrets_dir / SECRETS_API_KEY_FILENAME,
    )


def child_env() -> dict[str, str]:
    """Environment for subprocesses: the caller's env plus the contract keys.

    Unrelated variables pass through untouched; the contract keys are set to
    the resolved values so every child resolves the same paths as the parent
    CLI, whatever its own cwd.
    """
    env = dict(os.environ)
    resolved = paths()
    values = (
        resolved.project_root,
        resolved.hf_cache,
        resolved.ffmpeg_bin,
        resolved.venv_python,
        resolved.pot_provider_dir,
        resolved.secrets_dir,
    )
    for key, value in zip(CONTRACT_KEYS, values):
        env[key] = str(value)
    return env
