"""The env contract resolves once, overrides win, and children inherit it."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aclip import env

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def test_overrides_beat_defaults(ready_project: dict[str, Path]) -> None:
    """An exported override wins over the same path derived from the root."""
    resolved = env.paths()
    assert resolved.project_root == ready_project["venv_python"].parents[2]
    assert resolved.hf_cache == ready_project["hf_cache"]
    assert resolved.ffmpeg_bin == ready_project["ffmpeg"]
    assert resolved.venv_python == ready_project["venv_python"]
    assert resolved.pot_provider_dir == ready_project["pot_provider"]
    assert resolved.secrets_dir == ready_project["secrets"]
    assert resolved.secrets_api_key == ready_project["secrets"] / "nvidia_api_key"


def _discovered_root() -> Path:
    """The contract's own way to name the project root: the directory that
    holds ``skills/``, walking up from the module under test."""
    for parent in Path(env.__file__).resolve().parents:
        if (parent / "skills").is_dir():
            return parent
    raise AssertionError("no checkout root above the aclip package")


def test_defaults_resolve_from_discovered_root(clean_contract_env: None) -> None:
    """Without overrides every path is derived from the discovered project root."""
    root = _discovered_root()
    assert (root / "skills").is_dir()
    assert root == env.paths().project_root
    assert env.paths().hf_cache == root / "models" / "hf-cache"
    assert env.paths().ffmpeg_bin == root / "tools" / "bin" / "ffmpeg"
    assert env.paths().venv_python == root / ".venv" / "bin" / "python"
    assert env.paths().pot_provider_dir == root / "tools" / "vendor" / "bgutil-ytdlp-pot-provider"
    assert env.paths().secrets_dir == root / ".secrets"


def test_discovery_is_cwd_independent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The same clone resolves identically from any working directory."""
    for cwd in (tmp_path, Path("/"), SCRIPTS_DIR):
        monkeypatch.chdir(cwd)
        monkeypatch.delenv("AUTOCLIP_PROJECT_DIR", raising=False)
        assert env.paths().project_root == _discovered_root()


def test_child_env_merges_without_clobbering(
    ready_project: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """child_env carries the contract keys and leaves unrelated env alone."""
    monkeypatch.setenv("ACLIP_UNRELATED_SENTINEL", "keep-me")
    child = env.child_env()
    resolved = env.paths()
    assert child["ACLIP_UNRELATED_SENTINEL"] == "keep-me"
    assert child["PROJECT_ROOT"] == str(resolved.project_root)
    assert child["HF_HUB_CACHE"] == str(resolved.hf_cache)
    assert child["FFMPEG_BIN"] == str(resolved.ffmpeg_bin)
    assert child["VENV_PYTHON"] == str(resolved.venv_python)
    assert child["POT_PROVIDER_DIR"] == str(resolved.pot_provider_dir)
    assert child["SECRETS_DIR"] == str(resolved.secrets_dir)
    # every contract key is present
    assert set(env.CONTRACT_KEYS) <= set(child)
    # and the parent environment is not mutated by the call
    assert os.environ["ACLIP_UNRELATED_SENTINEL"] == "keep-me"


def test_child_env_inherits_and_replaces(clean_contract_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """child_env starts from os.environ and replaces the contract keys in place."""
    monkeypatch.setenv("FFMPEG_BIN", "/nonexistent/ffmpeg")
    child = env.child_env()
    assert child["PATH"] == os.environ["PATH"]
    assert child["FFMPEG_BIN"] == "/nonexistent/ffmpeg"
