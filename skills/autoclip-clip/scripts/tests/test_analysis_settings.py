"""settings.md parsing, as the analysis contract consumes it (ticket #30).

The frozen defaults come from the tracked repo-root settings.md; the override
behaviour comes from a tmp file wired through ``AUTOCLIP_SETTINGS`` (the
settings module's own env override, re-read per call).
"""

from __future__ import annotations

import pytest

from aclip import settings


def test_tracked_settings_parse_to_frozen_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repo-root settings.md parses to every frozen default."""
    monkeypatch.delenv("AUTOCLIP_SETTINGS", raising=False)
    loaded = settings.load_settings()
    # model blank = the Coding Agent's own model (surfaced as None)
    assert loaded.model is None
    assert loaded.language == "en"
    assert loaded.similarity_threshold == 0.85
    assert loaded.parallelism == 1
    assert loaded.chunk_minutes == 6
    assert loaded.intro_sec == 90
    assert loaded.clip_min_sec == 30 and loaded.clip_max_sec == 90
    assert loaded.target_count == 10
    assert loaded.min_score is None
    assert loaded.speed == 1.3
    assert loaded.prefer == "whisper"  # ADR 0007: whisper wording is the default
    assert loaded.title_card["enabled"] is True
    assert loaded.title_card["duration_sec"] == 6
    assert loaded.title_card["box_opacity"] == 0.45
    assert loaded.title_card["font"] is None  # "font= (empty = bundled default)" gloss
    assert loaded.captions["style"] == "karaoke"
    assert loaded.captions["margin_v"] == 420
    assert loaded.captions["outline"] == 2


def test_blank_model_reads_as_none(analysis_settings) -> None:
    analysis_settings(model="")
    assert settings.load_settings().model is None


def test_model_override_wins_and_keeps_the_other_defaults(analysis_settings) -> None:
    analysis_settings(model="meta/llama-3.1-405b-instruct", chunk_minutes="4")
    loaded = settings.load_settings()
    assert loaded.model == "meta/llama-3.1-405b-instruct"
    assert loaded.chunk_minutes == 4
    assert loaded.language == "en"
    assert loaded.intro_sec == 90
    assert loaded.parallelism == 1


def test_the_em_dash_gloss_is_not_part_of_the_value(analysis_settings) -> None:
    analysis_settings(model="nvidia/override — only for this run")
    assert settings.load_settings().model == "nvidia/override"


def test_missing_file_raises_naming_the_full_path(analysis_settings) -> None:
    missing = analysis_settings()
    missing.unlink()
    with pytest.raises(FileNotFoundError) as excinfo:
        settings.load_settings()
    assert str(missing) in str(excinfo.value)


def test_settings_from_text_is_pure() -> None:
    """Parsing plain text touches neither the filesystem nor the environment."""
    loaded = settings.settings_from_text("model: some/override\nchunk_minutes: 3\n")
    assert loaded.model == "some/override"
    assert loaded.chunk_minutes == 3
    assert loaded.language == "en"  # missing keys fall back to the frozen defaults


def test_prefer_parses_the_compare_asks_three_answers(analysis_settings) -> None:
    """``prefer:`` carries the compare step's A whisper / B youtube / C both."""
    for answer in ("whisper", "youtube", "both"):
        analysis_settings(prefer=answer)
        assert settings.load_settings().prefer == answer
    analysis_settings(prefer="YouTube")
    assert settings.load_settings().prefer == "youtube"  # case-insensitive


def test_an_illegal_prefer_value_is_a_loud_defect(analysis_settings) -> None:
    """An unreadable answer must not silently look like a legal one."""
    analysis_settings(prefer="whatever")
    with pytest.raises(ValueError) as excinfo:
        settings.load_settings()
    assert "whisper, youtube, both" in str(excinfo.value)


def test_an_absent_prefer_key_defaults_to_whisper() -> None:
    """Older settings.md files without the key keep ADR 0007's default."""
    assert settings.settings_from_text("chunk_minutes: 3\n").prefer == "whisper"
