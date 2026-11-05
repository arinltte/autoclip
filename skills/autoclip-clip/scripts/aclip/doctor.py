"""``aclip doctor`` — checks the environment the skills need and says what is missing.

Every check returns a :class:`CheckResult` carrying a name, a pass flag and a
fix hint. A fully green environment prints nothing and exits 0, so a script
can gate on the exit status alone; any failure prints one ``FAIL <name> —
<hint>`` line per check and exits 1. No check ever raises: an unexpected
error becomes a failing check, never a traceback.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .env import WHISPER_MODEL_DIR_NAME, paths

TESTS_DIR = Path(__file__).resolve().parent.parent / "tests"

SETUP_HINT = "run the autoclip-setup skill (or tools/setup.sh) to repair it"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one environment check."""

    name: str
    ok: bool
    hint: str = ""


def _run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a helper binary, capturing output, without ever raising."""
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )


def check_venv() -> CheckResult:
    """The project venv python must exist, be executable and report a version."""
    python = paths().venv_python
    if not python.is_file():
        return CheckResult("venv", False, f"{python} is missing — {SETUP_HINT}")
    if not os.access(python, os.X_OK):
        return CheckResult("venv", False, f"{python} is not executable — {SETUP_HINT}")
    try:
        proc = _run([str(python), "--version"])
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("venv", False, f"{python} failed to run ({exc}) — {SETUP_HINT}")
    if proc.returncode != 0:
        return CheckResult("venv", False, f"{python} --version exited {proc.returncode} — {SETUP_HINT}")
    return CheckResult("venv", True)


def check_ffmpeg() -> CheckResult:
    """The project ffmpeg must exist, be executable and carry the drawtext filter.

    A lean system ffmpeg (Homebrew's, for instance) lacks libass/freetype and
    so has no drawtext — text rendering would silently fail at render time.
    """
    ffmpeg = paths().ffmpeg_bin
    if not ffmpeg.is_file():
        return CheckResult("ffmpeg", False, f"{ffmpeg} is missing — {SETUP_HINT}")
    if not os.access(ffmpeg, os.X_OK):
        return CheckResult("ffmpeg", False, f"{ffmpeg} is not executable — {SETUP_HINT}")
    try:
        proc = _run([str(ffmpeg), "-hide_banner", "-filters"])
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("ffmpeg", False, f"{ffmpeg} failed to run ({exc}) — {SETUP_HINT}")
    if "drawtext" not in proc.stdout:
        return CheckResult(
            "ffmpeg",
            False,
            f"{ffmpeg} has no drawtext filter (lean build?) — {SETUP_HINT}",
        )
    return CheckResult("ffmpeg", True)


# A whisper weight shard (mlx .safetensors, safetensors .bin, .pth, .gguf) is
# hundreds of MB; anything below this floor is a leftover fragment from an
# interrupted download. The first version of this check accepted ANY file in
# the snapshot, so a 9 KB stub (3 tiny blobs, no weights) copied in from a
# broken global cache passed the gate and only failed at transcribe time.
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pth", ".gguf")
MIN_WEIGHT_BYTES = 1 << 20  # 1 MB


def check_whisper_weights() -> CheckResult:
    """The whisper snapshot in the project HF cache must hold real weight files."""
    cache = paths().hf_cache
    model_dir = cache / WHISPER_MODEL_DIR_NAME / "snapshots"
    if not model_dir.is_dir():
        return CheckResult(
            "whisper-weights",
            False,
            f"{model_dir} is missing — download the whisper weights into the project cache "
            f"with the autoclip-setup skill (or tools/setup.sh)",
        )
    snapshots = sorted(p for p in model_dir.iterdir() if p.is_dir())
    if not snapshots:
        return CheckResult(
            "whisper-weights",
            False,
            f"{model_dir} has no snapshot — {SETUP_HINT}",
        )
    for snapshot in snapshots:
        for candidate in snapshot.rglob("*"):
            if candidate.is_file() and candidate.suffix in WEIGHT_SUFFIXES:
                if candidate.stat().st_size >= MIN_WEIGHT_BYTES:
                    return CheckResult("whisper-weights", True)
    return CheckResult(
        "whisper-weights",
        False,
        f"snapshot {snapshots[0]} holds no weight shard >= {MIN_WEIGHT_BYTES} bytes "
        f"(.safetensors/.bin/.pth/.gguf) — a stub from an interrupted download passes "
        f"no gate: warm the weights with the autoclip-setup skill (or tools/setup.sh)",
    )


def check_pot_server() -> CheckResult:
    """The PO-token provider must be vendored into the project."""
    provider = paths().pot_provider_dir
    if not provider.is_dir():
        return CheckResult(
            "pot-server",
            False,
            f"{provider} is missing — install the bgutil PO-token provider "
            f"with the autoclip-setup skill (or tools/setup.sh)",
        )
    return CheckResult("pot-server", True)


def check_secrets() -> CheckResult:
    """The NIM key file must be present. Presence only — never read or print it."""
    key_file = paths().secrets_api_key
    if not key_file.is_file():
        return CheckResult(
            "secrets",
            False,
            f"{key_file} is missing — place the NVIDIA NIM key there with the autoclip-setup "
            f"skill (or tools/setup.sh)",
        )
    return CheckResult("secrets", True)


def _under_pytest() -> bool:
    """True when this process was started by pytest.

    The suite calls ``doctor`` itself, so running the nested pytest from here
    would recurse forever. ``PYTEST_CURRENT_TEST`` is inherited by subprocess
    children, which also stops a nested run from re-entering.
    """
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


def check_tests() -> CheckResult:
    """The gate suite must pass. Skipped while the suite itself is running."""
    if _under_pytest():
        return CheckResult("tests", True)
    if not TESTS_DIR.is_dir():
        return CheckResult("tests", False, f"{TESTS_DIR} is missing — the test suite is part of the CLI")
    try:
        proc = _run([sys.executable, "-m", "pytest", str(TESTS_DIR), "-q"])
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("tests", False, f"could not run pytest ({exc}) — {SETUP_HINT}")
    if proc.returncode != 0:
        return CheckResult(
            "tests",
            False,
            "pytest reported failures — run "
            f"`{sys.executable} -m pytest {TESTS_DIR} -q` and read the output",
        )
    return CheckResult("tests", True)


CHECKS: tuple[Callable[[], CheckResult], ...] = (
    check_venv,
    check_ffmpeg,
    check_whisper_weights,
    check_pot_server,
    check_secrets,
    check_tests,
)


def run_checks() -> list[CheckResult]:
    """Run every check, turning an unexpected error into a failing check."""
    results: list[CheckResult] = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:  # a check must never produce a traceback
            results.append(CheckResult(check.__name__.removeprefix("check_"), False, f"{exc} — {SETUP_HINT}"))
    return results


def main(argv: Iterable[str] | None = None) -> int:
    """Entry point for ``aclip doctor``. Returns the process exit status."""
    results = run_checks()
    failures = [result for result in results if not result.ok]
    for result in failures:
        print(f"FAIL {result.name} — {result.hint}")
    if failures:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    sys.exit(main())
