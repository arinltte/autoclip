"""The download ladder + scaffold, the plan half pure (ticket #28).

The runner is injected — tests never spawn yt-dlp. The rung strings, the PO-token
extractor arg, the cached-token drop, the resume predicate, percent parsing and
error extraction are ported from the app era's ``src/download.rs``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aclip import download, scaffold, transcribe

WATCH_URL = "https://www.youtube.com/watch?v=X5HIcE2BRuc"
SHORT_URL = "https://www.youtube.com/shorts/_Ia-G4KKKks"
BE_URL = "https://youtu.be/X5HIcE2BRuc"


class FakeRunner:
    """A scripted runner: a list of (returncode, stdout) per invocation."""

    def __init__(self, scripted: list[tuple[int, str]], *, produces: Path | None = None) -> None:
        self.scripted = list(scripted)
        self.produces = produces
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], env: dict[str, str]) -> "FakeResult":
        self.calls.append(argv)
        rc, stdout = self.scripted.pop(0) if self.scripted else (0, "")
        if rc == 0 and self.produces is not None:
            self.produces.parent.mkdir(parents=True, exist_ok=True)
            self.produces.write_bytes(b"media")
        return FakeResult(rc, stdout)


class FakeResult:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    monkeypatch.setenv("AUTOCLIP_PROJECT_DIR", str(root))
    return root


def _dest(root: Path, video_id: str = "X5HIcE2BRuc") -> Path:
    return root / "projects" / video_id / "download" / f"{video_id}_hi.mp4"


# --- the ladder --------------------------------------------------------------------


def test_the_three_rungs_and_their_fixed_formats() -> None:
    ladder = download.build_ladder()
    assert [attempt.n for attempt in ladder] == [1, 2, 3]
    assert ladder[0].fmt == download.FORMAT_BEST_1080
    assert ladder[1].fmt == download.FORMAT_AVC_720 == ladder[2].fmt
    assert [attempt.clear_token_cache for attempt in ladder] == [False, False, True]


def test_the_po_token_extractor_arg_points_at_the_vendored_server(tmp_path: Path) -> None:
    pot = download.pot_extractor_arg(tmp_path)
    assert pot == f"youtubepot-bgutilscript:server_home={tmp_path / 'vendor' / 'bgutil-ytdlp-pot-provider' / 'server'}"


def test_download_args_are_the_exact_invocation(tmp_path: Path) -> None:
    dest, pot = tmp_path / "d.mp4", "youtubepot-bgutilscript:server_home=/x"
    args = download.download_args("SRC", dest, "FMT", pot)
    assert args[:5] == ["--no-update", "--extractor-args", pot, "-f", "FMT"]
    assert "--merge-output-format" in args and "mp4" in args
    assert args[-2:] == ["SRC", str(dest)] or args[-2:] == [str(dest), "SRC"]


def test_video_ids_extract_from_every_url_shape() -> None:
    assert download.extract_video_id(WATCH_URL) == "X5HIcE2BRuc"
    assert download.extract_video_id(SHORT_URL) == "_Ia-G4KKKks"
    assert download.extract_video_id(BE_URL) == "X5HIcE2BRuc"
    assert download.extract_video_id("/tmp/clip.mp4") == "clip"


# --- the plan-half helpers -----------------------------------------------------------


def test_downloaded_ok_and_stale_partials(tmp_path: Path) -> None:
    dest = tmp_path / "download" / "vid_hi.mp4"
    assert not download.downloaded_ok(dest)
    (tmp_path / "download").mkdir(parents=True)
    (tmp_path / "download" / "vid_hi.mp4.part").write_bytes(b"partial")
    (tmp_path / "download" / "vid_hi.mp4.f137.mp4").write_bytes(b"fragment")
    (tmp_path / "download" / "vid_hi.mp4").write_bytes(b"real")
    (tmp_path / "download" / "other.mp4").write_bytes(b"someone else")
    assert download.downloaded_ok(tmp_path / "download" / "vid_hi.mp4")
    partials = download.stale_partials(tmp_path / "download" / "vid_hi.mp4")
    assert [p.name for p in partials] == ["vid_hi.mp4.f137.mp4", "vid_hi.mp4.part"]
    assert download.clean_partials(tmp_path / "download" / "vid_hi.mp4") == 2
    assert download.downloaded_ok(tmp_path / "download" / "vid_hi.mp4")  # the real file stayed


def test_percent_parsing_and_error_extraction() -> None:
    assert download.parse_download_percent("[download]  45.6% of 12.31MiB at 3.20MiB/s") == (
        45.6,
        "downloading 45.6%",
    )
    assert download.parse_download_percent("[download] Destination: x.mp4") is None
    assert download.last_log_error(["noise", "ERROR: unable to download video data: HTTP Error 403"]) == (
        "unable to download video data: HTTP Error 403"
    )
    assert download.last_log_error(["only noise"]) is None


def test_clearing_one_videos_token_entry_leaves_the_rest(tmp_path: Path) -> None:
    cache = tmp_path / "bgutil-ytdlp-pot-provider"
    cache.mkdir()
    (cache / "cache.json").write_text(json.dumps({"X5HIcE2BRuc": "tok-a", "OTHER": "tok-b"}))
    assert download.clear_token_cache_entry_at(cache, "X5HIcE2BRuc") is True
    assert json.loads((cache / "cache.json").read_text()) == {"OTHER": "tok-b"}
    assert download.clear_token_cache_entry_at(cache, "X5HIcE2BRuc") is False  # already gone
    assert download.clear_token_cache_entry_at(tmp_path / "missing", "X") is False


# --- the ladder run (injected runner) -------------------------------------------------


def test_a_cached_download_is_the_resume_case(project_root: Path) -> None:
    dest = _dest(project_root)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"media")
    runner = FakeRunner([])  # no rung may run
    assert download.run_download("X5HIcE2BRuc", WATCH_URL, runner=runner, tools_dir=project_root / "tools") == dest
    assert runner.calls == []


def test_the_first_rung_success_stops_the_ladder(project_root: Path) -> None:
    runner = FakeRunner([(0, "[download] 100%")], produces=_dest(project_root))
    dest = download.run_download("X5HIcE2BRuc", WATCH_URL, runner=runner, tools_dir=project_root / "tools")
    assert dest == _dest(project_root) and len(runner.calls) == 1


def test_the_ladder_walks_down_and_falls_back_to_the_second_rung(project_root: Path) -> None:
    runner = FakeRunner(
        [(1, "ERROR: unable to download video data: HTTP Error 403: Forbidden"), (0, "[download] 100%")],
        produces=_dest(project_root),
    )
    dest = download.run_download("X5HIcE2BRuc", WATCH_URL, runner=runner, tools_dir=project_root / "tools")
    assert dest == _dest(project_root) and len(runner.calls) == 2
    assert download.FORMAT_AVC_720 in runner.calls[1]  # rung 2's format


def test_exhaustion_is_a_hard_failure_with_the_exact_last_error(project_root: Path) -> None:
    error_line = "ERROR: unable to download video data: HTTP Error 403: Forbidden"
    runner = FakeRunner([(1, error_line), (1, error_line), (1, error_line)])
    with pytest.raises(download.DownloadError) as excinfo:
        download.run_download("X5HIcE2BRuc", WATCH_URL, runner=runner, tools_dir=project_root / "tools")
    assert excinfo.value.last_error == "unable to download video data: HTTP Error 403: Forbidden"
    assert "HTTP Error 403" in str(excinfo.value)


def test_the_last_rung_drops_the_cached_po_token(project_root: Path) -> None:
    cleared: list[str] = []
    runner = FakeRunner([(1, "ERROR: unable to download video data: HTTP Error 403"), (1, "err"), (1, "err")])
    with pytest.raises(download.DownloadError):
        download.run_download(
            "X5HIcE2BRuc",
            WATCH_URL,
            runner=runner,
            clear_token_cache=cleared.append,
            tools_dir=project_root / "tools",
        )
    assert cleared == ["X5HIcE2BRuc"]  # once, on the third rung only


# --- scaffold ---------------------------------------------------------------------


def test_scaffold_creates_the_frozen_layout_idempotently(project_root: Path) -> None:
    first = scaffold.scaffold_project("X5HIcE2BRuc")
    assert first == project_root / "projects" / "X5HIcE2BRuc"
    for subdir in scaffold.PROJECT_SUBDIRS:
        assert (first / subdir).is_dir()
    second = scaffold.scaffold_project("X5HIcE2BRuc")  # re-run costs nothing
    assert second == first and sorted(p.name for p in second.iterdir()) == sorted(scaffold.PROJECT_SUBDIRS)


# --- the zero-speech hard failure --------------------------------------------------


def test_zero_speech_media_is_a_hard_failure() -> None:
    wordless = {"segments": [{"start": 0, "end": 2, "text": " [Music]"}]}
    with pytest.raises(ValueError, match="zero-speech"):
        transcribe.ensure_speech(wordless)
    speech = {"segments": [{"words": [{"word": "hi", "start": 0.0, "end": 0.4}]}]}
    assert transcribe.ensure_speech(speech) == 1
