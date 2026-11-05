"""The Whisper Track: cue rebuild port pins, the NaN filter, weights preflight (#27).

The cue fixtures are the app era's own golden bytes (its ``tests/fixtures/cues``
sample: a real 8-segment slice plus synthetic rule-coverage segments) — the port
must reproduce them byte for byte. Weights logic is tested through a resolve
injection mirroring the offline HF rules in ``aclip.weights``' docstring: tests
never touch the network or huggingface_hub.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from aclip import cues as cues_module
from aclip import transcribe, weights

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CUES_DIR = FIXTURES / "cues"


# --- the cue rebuild port (byte-exact against the app's golden fixtures) ----------


def test_the_committed_sample_rebuilds_to_the_pythons_bytes() -> None:
    words = json.loads((CUES_DIR / "sample.words.json").read_text(encoding="utf-8"))
    rebuilt = cues_module.rebuild_cues(words)
    assert cues_module.render_cues_json(rebuilt) == (CUES_DIR / "sample.cues.json").read_text(encoding="utf-8")
    assert cues_module.render_cues_srt(rebuilt) == (CUES_DIR / "sample.srt").read_text(encoding="utf-8")


def _seg(words: list[dict]) -> dict:
    return {"start": words[0]["start"], "end": words[-1]["end"], "words": words}


def _word(text: str, start: float, end: float) -> dict:
    return {"word": text, "start": start, "end": end}


def test_a_punct_word_breaks_only_after_1_2s() -> None:
    quick = cues_module.rebuild_cues_from_segments(
        [_seg([_word("Done.", 0.0, 0.5), _word("Next", 0.5, 1.0)])]
    )
    assert len(quick) == 1  # a 0.5 s sentence is too short to break the cue
    slow = cues_module.rebuild_cues_from_segments(
        [_seg([_word("Done.", 0.0, 1.3), _word("Next", 1.3, 2.0)])]
    )
    assert [cue.text for cue in slow] == ["Done.", "Next"]


def test_the_char_limit_flushes_before_the_second_limit() -> None:
    words = [_word("word", 0.0 + 0.5 * i, 0.5 * (i + 1)) for i in range(20)]  # 4s / 100 chars
    built = cues_module.rebuild_cues_from_segments([_seg(words)])
    # the 38-char limit flushes first: no cue exceeds the 4.0 s limit
    assert all(cue.end - cue.start <= cues_module.MAX_DUR for cue in built)


def test_a_long_pause_breaks_the_cue() -> None:
    built = cues_module.rebuild_cues_from_segments(
        [_seg([_word("one", 0.0, 1.0), _word("two", 3.0, 4.0)])]  # 2 s gap > 1.2 s
    )
    assert [cue.text for cue in built] == ["one", "two"]


def test_whitespace_only_words_and_wordless_segments_are_skipped() -> None:
    built = cues_module.rebuild_cues_from_segments(
        [
            {"start": 0.0, "end": 1.0, "text": "no words here"},
            _seg([_word("  ", 0.0, 0.4), _word("real", 0.4, 1.0)]),
        ]
    )
    assert [cue.text for cue in built] == ["real"]


def test_a_word_without_timestamps_is_an_error() -> None:
    with pytest.raises(ValueError, match="no start timestamp"):
        cues_module.rebuild_cues_from_segments([{"start": 0.0, "end": 1.0, "words": [{"word": "x"}]}])
    with pytest.raises(ValueError, match='no "segments" array'):
        cues_module.rebuild_cues({"text": "no segments"})


def test_duration_stats_use_the_lower_middle_median() -> None:
    built = cues_module.rebuild_cues_from_segments(
        [
            _seg([_word("a", 0.0, 1.0)]),
            _seg([_word("b", 3.0, 5.0)]),  # 2 s pause gaps break the cues
            _seg([_word("c", 7.0, 10.0)]),
        ]
    )
    assert cues_module.cue_duration_stats(built) == (1.0, 2.0, 3.0)  # lower-middle median
    assert cues_module.cue_duration_stats([]) is None


# --- the NaN-words regression pin -------------------------------------------------


def test_non_finite_floats_become_none() -> None:
    payload = {"segments": [{"avg_logprob": float("nan")}, {"no_speech_prob": float("inf"), "ok": 1.5}]}
    cleaned = cues_module.non_finite_to_none(payload)
    assert cleaned["segments"][0]["avg_logprob"] is None
    assert cleaned["segments"][1]["no_speech_prob"] is None
    assert cleaned["segments"][1]["ok"] == 1.5


def test_json_dumps_writes_no_nan_literal() -> None:
    text = json.dumps(cues_module.non_finite_to_none({"v": float("nan")}))
    assert "NaN" not in text and text == '{"v": null}'


# --- the weights preflight (resolve-injected; no network) -------------------------


def _fake_cache(root: Path, model: str = "m", *, sha: str = "abc123") -> Path:
    tree = root / f"models--{model.replace('/', '--')}"
    (tree / "refs").mkdir(parents=True)
    (tree / "blobs").mkdir(parents=True)
    (tree / "snapshots" / sha).mkdir(parents=True)
    (tree / "blobs" / "weights").write_bytes(b"stub-weights")
    (tree / "snapshots" / sha / "weights.safetensors").symlink_to("../../blobs/weights")
    # refs written with printf (no trailing newline) — the offline rule hf applies
    (tree / "refs" / "main").write_text(sha)
    return tree


def _fake_resolve(model: str, cache_dir: Path | str) -> Path | None:
    """Mirror the offline HF resolution the module docstring pins: present ⇔
    refs/main (verbatim) points at a snapshots/<sha>/ dir holding real files."""
    tree = Path(cache_dir) / f"models--{model.replace('/', '--')}"
    ref = tree / "refs" / "main"
    if not ref.is_file():
        return None
    sha = ref.read_text()  # read WITHOUT stripping, like the hub does
    snapshot = tree / "snapshots" / sha
    if not snapshot.is_dir() or not any(p.is_file() or p.is_symlink() for p in snapshot.rglob("*")):
        return None
    return snapshot


def test_plan_is_none_when_the_project_cache_is_warm(tmp_path: Path) -> None:
    _fake_cache(tmp_path / "project")
    planned = weights.plan("m", tmp_path / "project", tmp_path / "global", resolve=_fake_resolve)
    assert planned.action == "none" and planned.path is not None


def test_plan_copies_from_the_global_cache(tmp_path: Path) -> None:
    global_root, project_root = tmp_path / "global", tmp_path / "project"
    tree = _fake_cache(global_root)
    planned = weights.plan("m", project_root, global_root, resolve=_fake_resolve)
    assert planned.action == "copy"
    assert planned.src == tree and planned.dst == project_root / tree.name


def test_plan_downloads_when_neither_cache_has_it(tmp_path: Path) -> None:
    planned = weights.plan("m", tmp_path / "project", tmp_path / "global", resolve=_fake_resolve)
    assert planned.action == "download" and planned.dst == tmp_path / "project"


def test_a_partial_project_tree_is_not_a_copy_source(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    tree = _fake_cache(project_root)  # a stale partial copy already in the project
    (tree / "refs" / "main").unlink()  # but its refs are absent → reads absent
    planned = weights.plan("m", project_root, tmp_path / "global", resolve=_fake_resolve)
    assert planned.action == "download"


def test_copy_model_preserves_the_hf_layout_and_resolves(tmp_path: Path) -> None:
    global_root, project_root = tmp_path / "global", tmp_path / "project"
    source = _fake_cache(global_root)
    copied = weights.copy_model(source, project_root)
    assert copied == project_root / source.name
    assert (copied / "snapshots" / "abc123" / "weights.safetensors").is_symlink()
    assert (copied / "refs" / "main").read_text() == "abc123"
    assert _fake_resolve("m", project_root) is not None


def test_copy_model_replaces_a_stale_destination(tmp_path: Path) -> None:
    global_root, project_root = tmp_path / "global", tmp_path / "project"
    source = _fake_cache(global_root)
    stale = project_root / source.name
    stale.mkdir(parents=True)
    (stale / "junk.txt").write_text("stale")
    copied = weights.copy_model(source, project_root)
    assert not (copied / "junk.txt").exists()


def test_ensure_warms_without_acting(tmp_path: Path) -> None:
    _fake_cache(tmp_path / "project")
    resolved = weights.ensure("m", project_cache=tmp_path / "project", global_cache=tmp_path / "global", resolve=_fake_resolve)
    assert resolved == tmp_path / "project" / "models--m" / "snapshots" / "abc123"


def test_ensure_copy_and_download_with_injected_executors(tmp_path: Path) -> None:
    global_root, project_root = tmp_path / "global", tmp_path / "project"
    _fake_cache(global_root)
    calls: list[str] = []

    def _copy(src, dst, on_progress=None):
        calls.append("copy")
        _fake_cache(project_root)
        return dst

    resolved = weights.ensure(
        "m", project_cache=project_root, global_cache=global_root,
        resolve=_fake_resolve, copy=_copy,
    )
    assert calls == ["copy"] and resolved is not None

    calls.clear()

    def _download(model, cache, on_progress=None):
        calls.append("download")
        _fake_cache(Path(cache))
        return Path(cache)

    weights.ensure(
        "m", project_cache=tmp_path / "empty", global_cache=tmp_path / "nothing",
        resolve=_fake_resolve, download=_download,
    )
    assert calls == ["download"]


def test_ensure_raises_when_the_model_still_does_not_resolve(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="did not resolve"):
        weights.ensure(
            "m", project_cache=tmp_path / "project", global_cache=tmp_path / "global",
            resolve=lambda *a, **k: None, download=lambda *a, **k: Path("."),
        )


# --- the transcribe seam ------------------------------------------------------------


def test_find_media_picks_the_single_downloaded_file(tmp_path: Path) -> None:
    (tmp_path / "download").mkdir()
    (tmp_path / "download" / "vid.mp4").write_bytes(b"x")
    (tmp_path / "download" / "vid.info.json").write_text("{}")  # sidecar, not media
    assert transcribe.find_media(tmp_path) == tmp_path / "download" / "vid.mp4"


def test_missing_media_names_the_download_step(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="run aclip download first"):
        transcribe.find_media(tmp_path)


# --- ticket #36: an existing Whisper Track is replayed, never re-run -------------


def test_an_existing_whisper_track_is_replayed_not_re_transcribed(
    ready_project: dict, analysis_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second run is free: the Whisper Track replays from the Project.

    The golden run (#36) acceptance — "no re-transcription" — found the CLI
    re-running mlx-whisper over 48 minutes of audio on every re-run. AGENTS.md
    says transcripts are never re-fetched; the Whisper Track now behaves like
    the download ladder's "already downloaded — skipped".
    """
    analysis_settings()  # the settings file the language default reads
    from aclip.env import paths

    project = paths().project_root / "projects" / "VID"
    (project / "transcript").mkdir(parents=True)
    (project / "transcript" / "VID_hi.words.json").write_text(
        json.dumps({"language": "en", "segments": [{"words": []}]}), encoding="utf-8"
    )
    (project / "transcript" / "VID_hi.cues.json").write_text("[]", encoding="utf-8")
    (project / "transcript" / "VID_hi.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("the Whisper Track already exists — never re-transcribe")

    monkeypatch.setattr(transcribe, "transcribe_media", _boom)

    summary = transcribe.transcribe_project("VID")

    assert summary["reused"] is True
    assert summary["word_count"] == 0 and summary["segments"] == 1
    assert summary["srt"] == project / "transcript" / "VID_hi.srt"
