"""Shared fixtures for the aclip gate suite.

The suite runs from either the repo root or the scripts directory and never
touches the network or the real project. ``ready_project`` builds a fake
checkout in ``tmp_path`` and injects the contract overrides so the code under
test resolves that folder instead of whatever checkout it happens to live in.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterator

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _write_executable(path: Path, body: str) -> Path:
    """Create an executable stub script, like a bundled python/ffmpeg."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if body.startswith("#!"):
        path.write_text(body)
    else:
        path.write_text(f"#!/bin/sh\n# stub\n{body}\n")
    path.chmod(0o755)
    return path


def build_ready_project(root: Path) -> dict[str, Path]:
    """Materialise a fully ready project layout under ``root``.

    Mirrors what the autoclip-setup skill produces: a venv python, the static
    ffmpeg with drawtext, the whisper snapshot in the project HF cache, the
    vendored PO-token provider and the NIM key file.
    """
    paths = {
        "venv_python": root / ".venv" / "bin" / "python",
        "ffmpeg": root / "tools" / "bin" / "ffmpeg",
        "hf_cache": root / "models" / "hf-cache",
        "pot_provider": root / "tools" / "vendor" / "bgutil-ytdlp-pot-provider",
        "secrets": root / ".secrets",
    }

    _write_executable(paths["venv_python"], "#!/bin/sh\necho 'Python 3.13.14'\nexit 0\n")
    _write_executable(
        paths["ffmpeg"],
        "\n".join(
            (
                "case \"$*\" in",
                '  *-filters*) printf "Filters:\\n T.. drawtext\\n TSC subtitle\\n"; exit 0 ;;',
                '  *) exit 0 ;;',
                "esac",
            )
        ),
    )

    snapshot = paths["hf_cache"] / "models--mlx-community--whisper-large-v3-turbo" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    # A real whisper shard is hundreds of MB; doctor's floor is 1 MB — a
    # 13-byte "stub-weights" would (correctly) fail the tightened check.
    (snapshot / "weights.safetensors").write_bytes(b"\0" * (1 << 21))

    paths["pot_provider"].mkdir(parents=True, exist_ok=True)
    (paths["pot_provider"] / "server.js").write_text("// stub\n")

    paths["secrets"].mkdir(parents=True)
    (paths["secrets"] / "nvidia_api_key").write_text("stub-key-never-printed\n")

    return paths


@pytest.fixture
def ready_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A fake ready project plus the env overrides that point the CLI at it."""
    paths = build_ready_project(tmp_path)
    monkeypatch.setenv("AUTOCLIP_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("HF_HUB_CACHE", str(paths["hf_cache"]))
    monkeypatch.setenv("FFMPEG_BIN", str(paths["ffmpeg"]))
    monkeypatch.setenv("VENV_PYTHON", str(paths["venv_python"]))
    monkeypatch.setenv("POT_PROVIDER_DIR", str(paths["pot_provider"]))
    monkeypatch.setenv("SECRETS_DIR", str(paths["secrets"]))
    return paths


@pytest.fixture
def clean_contract_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove every contract override so defaults resolve from the real root."""
    for name in (
        "AUTOCLIP_PROJECT_DIR",
        "HF_HUB_CACHE",
        "FFMPEG_BIN",
        "VENV_PYTHON",
        "POT_PROVIDER_DIR",
        "SECRETS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def run_cli() -> Iterator:
    """Run ``python -m aclip`` in the scripts dir and return (rc, stdout, stderr)."""
    import subprocess

    def _run(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "aclip", *args],
            cwd=str(cwd or SCRIPTS_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=600,
            env=dict(os.environ),
        )
        return proc.returncode, proc.stdout, proc.stderr

    yield _run


# --- ticket #30: the analysis contract's fixtures --------------------------------

_ANALYSIS_SETTINGS_DEFAULTS: dict[str, str] = {
    "model": "",
    "language": "en",
    "similarity_threshold": "0.85",
    "parallelism": "1",
    "chunk_minutes": "6",
    "intro_sec": "90",
    "clip_min_sec": "30",
    "clip_max_sec": "90",
    "target_count": "10",
    "min_score": "none",
    "speed": "1.3",
    "title_card": (
        "enabled=true, duration_sec=6, fontsize=46, box=true, box_opacity=0.45, "
        "font=, fontcolor=#ffffff, kicker=HIGHLIGHT, kicker_color=#ffd24a"
    ),
    "captions": (
        "enabled=true, style=karaoke, fontsize=54, spoken_color=#ffff00, "
        "unspoken_color=#ffffff, outline=2, margin_v=420"
    ),
}


def _analysis_settings_text(**overrides: object) -> str:
    """settings.md text with the frozen defaults, then the given overrides."""
    values = {**_ANALYSIS_SETTINGS_DEFAULTS, **{k: str(v) for k, v in overrides.items()}}
    return "\n".join(f"{k}: {v}" for k, v in values.items()) + "\n"


@pytest.fixture
def analysis_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write a tmp settings.md (frozen defaults + overrides) and wire the env."""

    def _make(**overrides: object) -> Path:
        path = tmp_path / "settings.md"
        path.write_text(_analysis_settings_text(**overrides), encoding="utf-8")
        monkeypatch.setenv("AUTOCLIP_SETTINGS", str(path))
        return path

    return _make


@pytest.fixture
def analysis_project(tmp_path: Path) -> Path:
    """A bare project dir with the frozen tree's transcript/ and analyze/."""
    project = tmp_path / "proj"
    (project / "transcript").mkdir(parents=True)
    (project / "analyze").mkdir(parents=True)
    return project


@pytest.fixture
def run_analysis() -> Iterator:
    """Run ``python -m aclip.analysis`` in the scripts dir; return (rc, out, err)."""
    import subprocess

    def _run(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "aclip.analysis", *args],
            cwd=str(cwd or SCRIPTS_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=600,
            env=dict(os.environ),
        )
        return proc.returncode, proc.stdout, proc.stderr

    yield _run
