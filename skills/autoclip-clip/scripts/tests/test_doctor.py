"""``aclip doctor``: silent green, loud red — one FAIL line naming the check.

The suite drives doctor as a subprocess (``python -m aclip doctor``) from the
scripts dir, exactly as the gate command does, so the reported row is the real
CLI process status. While the suite runs, doctor's own ``tests`` check is
cheapened to a pass: the interpreter that launched it is pytest, and re-running
the suite from inside the suite would recurse forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aclip import doctor

# check name -> break it by removing this piece of the ready project.
BREAKAGES: dict[str, str] = {
    "venv": "venv_python",
    "ffmpeg": "ffmpeg",
    "whisper-weights": "hf_cache",
    "pot-server": "pot_provider",
    "secrets": "secrets",
}


def test_ready_project_is_silent_and_green(ready_project: dict[str, Path], run_cli) -> None:
    """A fully ready project prints nothing and exits 0."""
    for entry in ready_project.values():
        assert entry.exists()
    code, out, err = run_cli("doctor")
    assert code == 0, f"stdout={out!r} stderr={err!r}"
    assert out == ""
    assert err == ""


@pytest.mark.parametrize("check_name", sorted(BREAKAGES))
def test_each_missing_piece_fails(ready_project: dict[str, Path], run_cli, check_name: str) -> None:
    """Removing one piece makes that check fail and exit non-zero."""
    import shutil

    target = ready_project[BREAKAGES[check_name]]
    (target.unlink() if target.is_file() else shutil.rmtree(target))

    code, out, err = run_cli("doctor")
    assert code != 0
    assert f"FAIL {check_name} —" in out + err
    # the hint must point at the repair path
    assert "setup" in (out + err).lower()


def test_ffmpeg_without_drawtext_fails(ready_project: dict[str, Path], run_cli) -> None:
    """A lean ffmpeg (no drawtext) is a failure even though the binary runs."""
    ffmpeg = ready_project["ffmpeg"]
    ffmpeg.write_text("#!/bin/sh\nexit 0\n")
    ffmpeg.chmod(0o755)

    code, out, err = run_cli("doctor")
    assert code != 0
    assert "FAIL ffmpeg —" in out + err


def test_venv_that_cannot_run_fails(ready_project: dict[str, Path], run_cli) -> None:
    """A venv python that exits non-zero on --version is a failure."""
    venv = ready_project["venv_python"]
    venv.write_text("#!/bin/sh\nexit 3\n")
    venv.chmod(0o755)

    code, out, err = run_cli("doctor")
    assert code != 0
    assert "FAIL venv —" in out + err


def test_empty_snapshot_directory_fails(ready_project: dict[str, Path], run_cli) -> None:
    """A snapshot directory with no weight files is not a warm cache."""
    snapshot = (
        ready_project["hf_cache"]
        / "models--mlx-community--whisper-large-v3-turbo"
        / "snapshots"
        / "abc123"
    )
    (snapshot / "weights.safetensors").unlink()

    code, out, err = run_cli("doctor")
    assert code != 0
    assert "FAIL whisper-weights —" in out + err


def test_stub_weight_snapshot_fails(ready_project: dict[str, Path], run_cli) -> None:
    """A stub shard (a fragment from an interrupted download) is a failure.

    The first version of this check passed on ANY file in the snapshot, so a
    300-byte copy of a broken global-cache fragment looked warm and only
    failed at transcribe time. A snapshot must hold a real shard.
    """
    snapshot = (
        ready_project["hf_cache"]
        / "models--mlx-community--whisper-large-v3-turbo"
        / "snapshots"
        / "abc123"
    )
    (snapshot / "weights.safetensors").write_bytes(b"fragment\n")

    code, out, err = run_cli("doctor")
    assert code != 0
    assert "FAIL whisper-weights —" in out + err


def test_doctor_inside_pytest_does_not_recurse(ready_project: dict[str, Path]) -> None:
    """Called from the suite, the tests check must not spawn pytest again."""
    assert doctor._under_pytest()
    result = doctor.check_tests()
    assert result.name == "tests"
    assert result.ok


def test_doctor_main_returns_status_without_raising(ready_project: dict[str, Path]) -> None:
    """doctor.main returns an int; SystemExit is left to the caller."""
    assert doctor.main() == 0
    import shutil

    shutil.rmtree(ready_project["secrets"])
    assert doctor.main() == 1


def test_run_checks_never_raise_on_unexpected_error(monkeypatch) -> None:
    """A check that explodes becomes a failing check, not a traceback."""

    def boom() -> doctor.CheckResult:
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "CHECKS", (boom,))
    results = doctor.run_checks()
    assert len(results) == 1
    assert not results[0].ok
    assert "boom" in results[0].hint
