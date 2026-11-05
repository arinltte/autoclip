"""The YouTube Track fetch: language resolution, markers, output shape (ticket #25).

No network: the golden track lists are real, fetched once at implementation time
and stored; the selection and writing rules run over them. The library import is
lazy (the aclip core stays stdlib-only), asserted structurally here too.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from aclip import transcript_fetch

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def golden_lists() -> dict:
    return json.loads((FIXTURES / "golden_track_lists.json").read_text(encoding="utf-8"))


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    project = tmp_path / "X5HIcE2BRuc"
    (project / "transcript").mkdir(parents=True)
    return project


# --- language resolution over the golden track lists ----------------------------


def test_golden_track_counts_match_the_spec(golden_lists: dict) -> None:
    """The stored lists are the real ones: 5 tracks / 19 tracks."""
    assert golden_lists["X5HIcE2BRuc"]["track_count"] == 5
    assert len(golden_lists["X5HIcE2BRuc"]["tracks"]) == 5
    assert golden_lists["_Ia-G4KKKks"]["track_count"] == 19
    assert len(golden_lists["_Ia-G4KKKks"]["tracks"]) == 19


@pytest.mark.parametrize("video", ["X5HIcE2BRuc", "_Ia-G4KKKks"])
def test_settings_language_resolves_on_both_golden_videos(golden_lists: dict, video: str) -> None:
    chosen = transcript_fetch.choose_track(golden_lists[video]["tracks"], "en")
    assert chosen is not None
    assert chosen["language_code"] == "en"
    assert chosen["language"] == "English (auto-generated)"


def test_a_language_without_a_track_means_no_captions(golden_lists: dict) -> None:
    """Malay has no track on X5HIcE2BRuc — the run goes whisper-only, silently."""
    assert transcript_fetch.choose_track(golden_lists["X5HIcE2BRuc"]["tracks"], "ms") is None


def test_regional_variants_resolve_after_an_exact_miss() -> None:
    tracks = [
        {"language": "English (auto-generated)", "language_code": "en-US", "is_generated": True, "is_translatable": False},
        {"language": "English", "language_code": "en-GB", "is_generated": False, "is_translatable": True},
    ]
    chosen = transcript_fetch.choose_track(tracks, "en")
    # no exact "en": the regional fallback pool resolves, manual beats generated
    assert chosen["language_code"] == "en-GB"


def test_a_manual_track_beats_a_generated_one() -> None:
    auto = {"language": "English (auto-generated)", "language_code": "en", "is_generated": True, "is_translatable": True}
    manual = {"language": "English", "language_code": "en", "is_generated": False, "is_translatable": True}
    assert transcript_fetch.choose_track([auto, manual], "en") is manual


# --- what lands in the Project ---------------------------------------------------


def test_write_youtube_track_shape(tmp_project: Path) -> None:
    track = {"language": "English (auto-generated)", "language_code": "en", "is_generated": True}
    path = transcript_fetch.write_youtube_track(
        tmp_project,
        "X5HIcE2BRuc",
        track,
        [{"text": " hello world ", "start": 12.5, "duration": 3.0}],
    )
    assert path == tmp_project / "transcript" / "youtube_track.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["line_count"] == 1
    assert payload["lines"][0] == {"text": " hello world ", "start": 12.5, "duration": 3.0}
    assert payload["language_code"] == "en" and payload["is_generated"] is True


def test_no_captions_writes_the_explicit_empty_marker(tmp_project: Path) -> None:
    path = transcript_fetch.write_empty_marker(tmp_project, "X5HIcE2BRuc", "ms")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["empty"] is True and payload["lines"] == [] and payload["line_count"] == 0
    assert "whisper-only" in payload["note"]


# --- the lazy sidecar import -----------------------------------------------------


def test_importing_the_fetch_module_pulls_no_third_party_library() -> None:
    """The aclip core is stdlib-only; the venv library is imported on use."""
    assert "youtube_transcript_api" not in sys.modules


def test_missing_library_and_venv_raise_the_setup_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # sys.modules[...] = None makes the import itself raise ImportError, even
    # though the dev venv carries the library — the re-exec path is exercised
    # as if on a bare interpreter.
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", None)
    monkeypatch.setattr(
        transcript_fetch,
        "paths",
        lambda: type("P", (), {"venv_python": tmp_path / "no-python"})(),
    )
    with pytest.raises(transcript_fetch.NoTracksError) as excinfo:
        transcript_fetch._api_module()
    assert "autoclip-setup skill" in str(excinfo.value)


# --- ticket #36: an existing YouTube Track is replayed, never re-fetched --------


@pytest.fixture
def env_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project dir wired through the env contract (project_dir_for resolves it)."""
    monkeypatch.setenv("AUTOCLIP_PROJECT_DIR", str(tmp_path))
    return transcript_fetch.project_dir_for("VID")


def test_an_existing_youtube_track_is_replayed_not_refetched(
    env_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second run is free: the YouTube Track replays from the Project.

    The golden run (#36) acceptance — transcripts are never re-fetched — found
    the CLI hitting YouTube again on every re-run. Replay is keyed to the
    configured language, so a language change still fetches.
    """
    track = {"language": "English (auto-generated)", "language_code": "en", "is_generated": True}
    path = transcript_fetch.write_youtube_track(
        env_project, "VID", track, [{"text": "hi", "start": 0.0, "duration": 1.0}]
    )

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("the YouTube Track already exists — never re-fetch")

    monkeypatch.setattr(transcript_fetch, "list_tracks", _boom)

    code, returned = transcript_fetch.fetch_to_project("VID", language="en")

    assert code == 0 and returned == path


def test_a_different_language_is_not_replayed(env_project: Path) -> None:
    """The replay is keyed to the configured language — en cached, ms asked."""
    track = {"language": "English (auto-generated)", "language_code": "en", "is_generated": True}
    transcript_fetch.write_youtube_track(env_project, "VID", track, [])
    assert transcript_fetch.track_replayable(transcript_fetch.youtube_track_path(env_project), "ms") is False
    assert transcript_fetch.track_replayable(transcript_fetch.youtube_track_path(env_project), "en") is True


def test_the_empty_marker_replays_too(env_project: Path) -> None:
    """A video with no captions records the marker once and replays it."""
    path = transcript_fetch.write_empty_marker(env_project, "VID", "ms")
    assert transcript_fetch.track_replayable(path, "ms") is True
