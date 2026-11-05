"""The render port: the apostrophe-safe concat list, karaoke ASS, the animated
title card, the filter-graph plan, and the one parallel stage (ticket #29).

The runner is injected — ffmpeg never executes. Concurrency is proven with a
barrier in the fake runner: the calls must genuinely overlap.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from aclip import render

SETTINGS = {
    "speed": 1.3,
    "title": {
        "enabled": True, "duration_sec": 6, "fontsize": 46, "box": True,
        "box_opacity": 0.45, "font": "", "fontcolor": "#ffffff",
        "kicker": "HIGHLIGHT", "kicker_color": "#ffd24a",
        "max_lines": 2, "padding_pct": 6, "min_fontscale": 0.7, "max_chars": None,
    },
    "caption": {
        "enabled": True, "style": "karaoke", "fontsize": 54,
        "spoken_color": "#ffff00", "unspoken_color": "#ffffff", "outline": 2, "margin_v": 420,
    },
}

WORDS = [
    {"word": "Aunt's", "start": 10.0, "end": 10.4},
    {"word": "back", "start": 10.4, "end": 10.8},
    {"word": "again", "start": 10.8, "end": 11.2},
]
SRT = """1
00:00:10,000 --> 00:00:12,000
Aunt's back again

"""


# --- the colour helpers + the apostrophe-safe concat list (PATCH-5) ----------------


def test_hex_conversions() -> None:
    assert render.hex_to_ffmpeg("#ffff00") == "0xFFFF00"
    assert render.hex_to_ass("#FFFF00") == "&H0000FFFF"  # ASS is BGR
    assert render.hex_to_ass("#ffffff") == "&H00FFFFFF"


def test_concat_list_escapes_apostrophes_the_patch5_way(tmp_path: Path) -> None:
    """The regression re-pinned: the demuxer's quoting is '\\'' — an Aunt's
    title must not kill the collection."""
    a, b = tmp_path / "01 Aunt's moment.mp4", tmp_path / "plain.mp4"
    lines = render.concat_list_lines([a, b])
    quote = "'"
    expected = str(a.absolute()).replace(quote, quote + "\\" + quote + quote)
    assert lines[0] == f"file '{expected}'"  # Aunt\'s — shell-style '\'' quoting
    assert lines[1] == f"file '{b.absolute()}'"


def test_escape_filter_path_resolves_and_escapes(tmp_path: Path) -> None:
    path = tmp_path / "Aunt's: 99%.ass"
    path.write_text("x", encoding="utf-8")
    escaped = render.escape_filter_path(path)
    assert escaped.startswith("/") and "\\:" in escaped and "\\'" in escaped


# --- the title card -------------------------------------------------------------------


def test_title_exprs_fade_in_hold_fade_out() -> None:
    exprs = render.title_exprs({"duration_sec": 6})
    assert "t\\,0.5" in exprs.alpha and "6.00" in exprs.alpha
    assert "5.40" in exprs.alpha  # hold = dur - 0.6
    assert exprs.enable == "lt(t\\,6.20)"
    assert "h*0.08" in exprs.y


def test_title_chain_chains_two_labelled_drawtexts_with_a_kicker(tmp_path: Path) -> None:
    chain = render.build_title_chain("base", "Aunt's 99% Comeback", None, SETTINGS, tmp_path / "title.txt")
    assert len(chain.parts) == 2
    assert chain.parts[0].startswith("[base]drawtext=") and "expansion=none" in chain.parts[0]
    assert "[t1]drawtext=" in chain.parts[1] and chain.parts[1].endswith("[tout]")
    assert chain.title_body == "Aunt's 99% Comeback"
    assert "text='HIGHLIGHT'" in chain.parts[1]


def test_no_kicker_relabels_the_main_drawtext_to_tout(tmp_path: Path) -> None:
    settings = json.loads(json.dumps(SETTINGS))
    settings["title"]["kicker"] = ""
    chain = render.build_title_chain("base", "Title", None, settings, tmp_path / "title.txt")
    assert len(chain.parts) == 1 and chain.parts[0].endswith("[tout]")


# --- the title card's wrap (#38: a long title overflowed the frame's edges) ----


def test_budget_chars_leaves_the_padding_clear() -> None:
    # 1080 wide, 6% padding each side: 950.8 usable / (46 * 0.62) = 33
    assert render.budget_chars(1080, 46) == 33
    assert render.budget_chars(1080, 46, padding_pct=0) == 37
    assert render.budget_chars(1080, 46, max_chars=12) == 12  # the override wins


def test_wrap_title_keeps_words_and_never_drops_a_character() -> None:
    lines = render.wrap_title("Can't pay 5,000 ringgit? \"Just go home and wait to die\"", 33)
    assert lines == ["Can't pay 5,000 ringgit? \"Just go", "home and wait to die\""]
    assert " ".join(lines).split() == "Can't pay 5,000 ringgit? \"Just go home and wait to die\"".split()


def test_wrap_title_hard_splits_one_unbreakable_word() -> None:
    lines = render.wrap_title("supercalifragilistic", 10)
    assert lines == ["supercalif", "ragilistic"]  # no character lost, no line over


def test_wrap_title_collapses_whitespace_and_handles_empty() -> None:
    assert render.wrap_title("  a   b  ", 6) == ["a b"]
    assert render.wrap_title("   ", 6) == []


def test_fit_title_wraps_long_titles_and_shrinks_only_when_needed() -> None:
    body, size = render.fit_title("Short title", 1080, 46, max_lines=2)
    assert body == "Short title" and size == 46  # no wrap, no shrink

    # the real session's clip 01 title: 55 chars -> two lines at the full size
    body, size = render.fit_title(
        "Can't pay 5,000 ringgit? \"Just go home and wait to die\"", 1080, 46, max_lines=2
    )
    assert body.count("\n") == 1 and size == 46
    for line in body.split("\n"):
        assert len(line) <= 33

    # the real session's clip 10 title: 80 chars -> two lines, font shrunk
    body, size = render.fit_title(
        "Malaysia's public services are actually excellent — we just compare to Singapore",
        1080, 46, max_lines=2,
    )
    assert body.count("\n") == 1 and size < 46 and size >= 32
    for line in body.split("\n"):
        assert len(line) <= render.budget_chars(1080, size)


def test_fit_title_still_wraps_at_the_floor_instead_of_truncating() -> None:
    title = "word " * 40  # far past two lines even shrunk
    body, size = render.fit_title(title, 1080, 46, max_lines=2)
    assert size == 32  # the floor, never lower
    assert body.count("\n") >= 2  # a third line beats a cut title
    assert all(line for line in body.split("\n"))


def test_fit_title_settings_reads_the_title_card_mapping() -> None:
    body, size = render.fit_title_settings("Can't pay 5,000 ringgit? \"Just go home and wait to die\"", 1080, SETTINGS["title"])
    assert body.count("\n") == 1 and size == 46


def test_title_chain_writes_the_wrapped_body_and_the_fitted_size(tmp_path: Path) -> None:
    chain = render.build_title_chain(
        "base", "Can't pay 5,000 ringgit? \"Just go home and wait to die\"", None, SETTINGS, tmp_path / "title.txt"
    )
    assert chain.title_body.count("\n") == 1  # two lines, not one clipped one
    assert chain.fontsize == 46
    assert f"fontsize={chain.fontsize}" in chain.parts[0]


def test_the_real_session_titles_all_fit_inside_the_padding() -> None:
    """The regression the run produced: every rendered title measured wider
    than the frame and was clipped on both edges. Now every line is inside the
    padding the card keeps clear."""
    titles = [
        'Can\'t pay 5,000 ringgit? "Just go home and wait to die"',
        "Two T20 families, identical income, completely different lives",
        "Retrenched at 40: the RM30k earner stuck with a house he can't sell",
        '"Hard work in this economy doesn\'t equate to higher income"',
        "Assets let the top 1% sit outside the income bracket entirely",
        "Why taxing the rich is a global problem — billionaires flee to Dubai",
        '"We have more cars than we have babies" — how cheap fuel shaped Malaysian life',
        "Controversial take: everyone should use less fuel, the rich should pay more",
        "Two M40 salaries marry and instantly become T20 — with more expenses",
        "Malaysia's public services are actually excellent — we just compare to Singapore",
    ]
    for title in titles:
        body, size = render.fit_title_settings(title, 1080, SETTINGS["title"])
        assert body.count("\n") <= 1, title  # at most two lines
        for line in body.split("\n"):
            assert len(line) <= render.budget_chars(1080, size), (title, line)


# --- the per-clip playback speed (#38: every clip was pegged to the ceiling) ---


def test_clip_speed_leaves_a_short_clip_alone() -> None:
    assert render.clip_speed(43.94, 1.3, 60) == 1.0
    assert render.clip_speed(60.0, 1.3, 60) == 1.0  # exactly at the target


def test_clip_speed_takes_the_smallest_step_that_fits() -> None:
    assert render.clip_speed(72.38, 1.3, 60) == 1.25  # 72.38 / 1.25 = 57.9 < 60
    assert render.clip_speed(65.0, 1.3, 60) == 1.1
    assert render.clip_speed(61.0, 1.3, 60) == 1.1


def test_clip_speed_caps_and_never_goes_below_one() -> None:
    assert render.clip_speed(90.0, 1.3, 60) == 1.3  # even the cap will not fit it
    assert render.clip_speed(300.0, 1.3, 60) == 1.3
    assert render.clip_speed(43.94, 0.8, 60) == 1.0  # a sub-1.0 setting floors at 1.0


def test_clip_speed_ignores_a_preset_with_no_target() -> None:
    assert render.clip_speed(120.0, 1.3, None) == 1.0


def test_clamp_speed_bounds_the_ceiling() -> None:
    assert render.clamp_speed(1.3) == 1.3
    assert render.clamp_speed(0.5) == 1.0
    assert render.clamp_speed(3.0) == 2.0
    assert render.clamp_speed(None) == 1.0


# --- the karaoke ASS ---------------------------------------------------------------------


def test_ass_style_line_carries_the_settings_values() -> None:
    line = render.ass_style_line(SETTINGS)
    assert line.startswith("Style: Cap,Helvetica,54,&H0000FFFF,&H00FFFFFF,")
    assert ",1,2,60,60,420,1\n" in line


def test_fmt_ass_time_is_centiseconds_with_even_ties() -> None:
    assert render.fmt_ass_time(3661.0) == "1:01:01.00"
    assert render.fmt_ass_time(0.5) == "0:00:00.50"


def test_parse_srt_rows() -> None:
    entries = render.parse_srt(SRT)
    assert len(entries) == 1
    assert entries[0].start_time == "00:00:10,000" and entries[0].text == "Aunt's back again"


def test_to_seconds() -> None:
    assert render.to_seconds("00:00:10,500") == 10.5
    assert render.to_seconds("1:02:03.250") == 3723.25
    with pytest.raises(ValueError):
        render.to_seconds("not a time")


def test_build_ass_karaoke_from_word_timings_clip_local_and_sped() -> None:
    entries = render.parse_srt(SRT)
    words = render.load_words({"segments": [{"words": WORDS}]})
    ass = render.build_ass(entries, words, 10.0, 12.0, SETTINGS, speed=1.0)
    assert ass.startswith(render.ASS_HEADER)
    dialogue = [line for line in ass.splitlines() if line.startswith("Dialogue:")][0]
    # three {\k..} fills, min 1 cs each, the trailing space kept on each span
    assert dialogue.count("{\\k") == 3
    assert "{\\fad(120,150)" in dialogue
    assert dialogue.split(",")[3] == "Cap"  # Layer,Start,End,Style


def test_build_ass_scales_the_timeline_by_speed() -> None:
    entries = render.parse_srt(SRT)
    words = render.load_words({"segments": [{"words": WORDS}]})
    slow = render.build_ass(entries, words, 10.0, 12.0, SETTINGS, speed=1.0)
    fast = render.build_ass(entries, words, 10.0, 12.0, SETTINGS, speed=2.0)
    slow_end = re.search(r"Dialogue: 0,([\d:.]+),([\d:.]+),", slow).group(2)
    fast_end = re.search(r"Dialogue: 0,([\d:.]+),([\d:.]+),", fast).group(2)
    assert render.fmt_ass_time(render.to_seconds(fast_end)) < render.fmt_ass_time(render.to_seconds(slow_end))


def test_build_ass_line_level_when_not_karaoke() -> None:
    entries = render.parse_srt(SRT)
    words = render.load_words({"segments": [{"words": WORDS}]})
    settings = json.loads(json.dumps(SETTINGS))
    settings["caption"]["style"] = "line"
    ass = render.build_ass(entries, words, 10.0, 12.0, settings, speed=1.0)
    assert "{\\k" not in ass and "Aunt's back again" in ass


def test_cues_outside_the_clip_are_skipped() -> None:
    entries = render.parse_srt(SRT) + [render.SrtEntry("00:01:00,000", "00:01:02,000", "far away")]
    words = render.load_words({"segments": [{"words": WORDS}]})
    ass = render.build_ass(entries, words, 10.0, 12.0, SETTINGS, speed=1.0)
    assert ass.count("Dialogue") == 1


# --- the filter-graph plan ------------------------------------------------------------------


def _plan(tmp_path: Path, span: tuple[float, float] = (10.0, 82.0), **overrides) -> render.RenderPlan:
    settings = json.loads(json.dumps(SETTINGS))
    for key, value in overrides.items():
        if key in ("speed",):
            settings[key] = value
        else:
            settings.get(key, {}).update(value) if isinstance(value, dict) else None
    font = Path("/System/Library/Fonts/PingFang.ttc")
    return render.plan_render(
        span[0], span[1], "Aunt's Comeback", settings,
        render.parse_srt(SRT), render.load_words({"segments": [{"words": WORDS}]}),
        font if font.is_file() else None,
        tmp_path / "src.mp4", tmp_path / "out.mp4", tmp_path / "tmp",
    )


def test_the_graph_splits_blurs_and_overlays_before_the_text_filters(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    graph = plan.args[plan.args.index("-filter_complex") + 1]
    assert "[0:v]split=2[bg][fg]" in graph
    assert "crop=1080:1920,gblur=sigma=24" in graph
    assert "overlay=(W-w)/2:(H-h)/2[base]" in graph
    # 72 s over the 60 s target -> the speed step is derived per clip (1.2,
    # the smallest step that fits: 72 / 1.2 = 60.0), and setpts compresses the
    # timeline before the text filters
    assert plan.speed == 1.2
    assert "[base]setpts=PTS/1.2[spd]" in graph
    assert "[spd]ass='" in graph
    assert "drawtext=" in graph
    assert plan.duration_sec == 60.0  # 72 / 1.2, round(2)


def test_a_clip_that_fits_the_target_renders_at_natural_speed(tmp_path: Path) -> None:
    # 44 s is already under the 60 s target: no setpts, no atempo, unchanged
    plan = _plan(tmp_path, span=(10.0, 54.0))
    graph = plan.args[plan.args.index("-filter_complex") + 1]
    assert plan.speed == 1.0
    assert "setpts" not in graph and "atempo" not in str(plan.args) and "-af" not in plan.args
    assert plan.duration_sec == 44.0


def test_the_args_carry_three_decimal_ss_and_atempo_at_the_c_codec(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    assert plan.args[plan.args.index("-ss") + 1] == "10.000"
    assert plan.args[plan.args.index("-t") + 1] == "72.000"
    assert plan.args[plan.args.index("-map") + 1] == "[tout]"
    audio_index = plan.args.index("-c:a")
    assert plan.args[audio_index - 2 : audio_index] == ["-af", "atempo=1.2"]  # the per-clip speed
    assert plan.args[audio_index:] == ["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-y", str(tmp_path / "out.mp4")]


def test_a_setting_below_the_ceiling_still_caps_the_ladder(tmp_path: Path) -> None:
    # a lower cap can never slow a clip below its natural speed
    plan = _plan(tmp_path, **{"speed": 1.1})
    assert plan.speed == 1.1
    assert "PTS/1.1" in plan.args[plan.args.index("-filter_complex") + 1]


def test_speed_one_omits_setpts_and_atempo(tmp_path: Path) -> None:
    plan = _plan(tmp_path, span=(10.0, 54.0))  # a clip that needs no speed-up
    graph = plan.args[plan.args.index("-filter_complex") + 1]
    # NB: check the PTS filter itself, not the substring "setpts" — the tmp
    # path embeds this test's name.
    assert "PTS/" not in graph and "-af" not in plan.args
    assert plan.duration_sec == 44.0


def test_captions_disabled_drops_the_ass_filter(tmp_path: Path) -> None:
    plan = _plan(tmp_path, **{"caption": {"enabled": False}})
    graph = plan.args[plan.args.index("-filter_complex") + 1]
    assert "ass=" not in graph and plan.ass_body is None
    # the title card still chains off the speed-scaled stream (speed 1.25 here)
    assert "[spd]drawtext=" in graph


def test_title_disabled_ends_the_chain_at_the_captions(tmp_path: Path) -> None:
    plan = _plan(tmp_path, **{"title": {"enabled": False}})
    assert plan.title_chain is None
    assert plan.args[plan.args.index("-map") + 1] == "[sub]"


def test_clip_title_and_slug() -> None:
    assert render.clip_title({"generated_title": "Aunt's Comeback"}) == "Aunt's Comeback"
    assert render.clip_title({"title": "", "outline": "From outline"}) == "From outline"
    assert render.clip_title({}) == "clip?"
    assert render.slug("Aunt's 99% Comeback!!") == "Aunt_s_99__Comeback__"  # the ported rule keeps underscores as-is


# --- the driver + the one parallel stage -------------------------------------------------


class FakeRunner:
    """Records calls; a barrier proves the calls genuinely overlap."""

    def __init__(self, *, barrier: threading.Barrier | None = None, produces: bool = True) -> None:
        self.barrier = barrier
        self.produces = produces
        self.calls: list[list[str]] = []
        self.lock = threading.Lock()

    def __call__(self, argv: list[str], env: dict[str, str]) -> "FakeResult":
        with self.lock:
            self.calls.append(argv)
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        if self.produces:
            Path(argv[-1]).write_bytes(b"rendered")
        return FakeResult(0, "")


class FakeResult:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def _plans(tmp_path: Path, count: int) -> list[render.RenderPlan]:
    font = Path("/System/Library/Fonts/PingFang.ttc")
    return [
        render.plan_render(
            float(10 + i), float(80 + i), f"Clip {i}", SETTINGS,
            render.parse_srt(SRT), render.load_words({"segments": [{"words": WORDS}]}),
            font if font.is_file() else None,
            tmp_path / "src.mp4", tmp_path / f"out{i}.mp4", tmp_path / f"tmp{i}",
        )
        for i in range(count)
    ]


def test_render_one_writes_the_ass_and_title_bodies(tmp_path: Path) -> None:
    plan = _plans(tmp_path, 1)[0]
    assert plan.ass_body and plan.title_chain
    row = render.render_one(plan, runner=FakeRunner())
    assert row["ok"] and Path(row["out_path"]).is_file()
    # the temp dir is cleaned up (the bodies are gone with it)


def test_render_all_renders_concurrently(tmp_path: Path) -> None:
    count = 3
    barrier = threading.Barrier(count, timeout=5)
    runner = FakeRunner(barrier=barrier)
    rows = render.render_all(_plans(tmp_path, count), runner=runner)
    assert [row["ok"] for row in rows] == [True] * count
    assert len(runner.calls) == count  # the barrier did not time out: truly concurrent


def test_render_all_reports_failures_without_raising(tmp_path: Path) -> None:
    runner = FakeRunner(produces=False)
    rows = render.render_all(_plans(tmp_path, 2), runner=runner)
    assert all(row["ok"] is False for row in rows)


def test_compile_args_and_the_preset_geometry() -> None:
    args = render.compile_args(Path("/x/list.txt"), Path("/x/out.mp4"))
    assert args[args.index("-f") + 1] == "concat" and "-c" in args and "copy" in args
    assert render.PRESETS[render.SHORTS_PRESET]["w"] == 1080


# --- ADR 0007: the compare step's remember_as prefer: (ticket #36 review fix) ----

YOUTUBE_TRACK = {
    "video_id": "VID",
    "language": "English (auto-generated)",
    "language_code": "en",
    "lines": [
        {"text": "Auntie's back again", "start": 10.0, "duration": 2.0},
        {"text": "and today we eat", "start": 12.0, "duration": 2.0},
    ],
}


def test_prefer_whisper_keeps_the_whisper_cues_and_word_timings() -> None:
    entries = render.parse_srt(SRT)
    words = render.load_words({"segments": [{"words": WORDS}]})
    cues, kept = render.caption_source("whisper", YOUTUBE_TRACK, entries, words)
    assert [c.text for c in cues] == ["Aunt's back again"]
    assert len(kept) == 3  # word timings survive → karaoke


def test_prefer_youtube_uses_the_track_wording_without_word_timings() -> None:
    entries = render.parse_srt(SRT)
    words = render.load_words({"segments": [{"words": WORDS}]})
    cues, kept = render.caption_source("youtube", YOUTUBE_TRACK, entries, words)
    assert [c.text for c in cues] == ["Auntie's back again", "and today we eat"]
    assert kept == []  # never invented — karaoke degrades to line-level
    ass = render.build_ass(cues, kept, 10.0, 12.0, SETTINGS, speed=1.0)
    assert "{\\k" not in ass and "Auntie's back again" in ass


def test_prefer_both_fills_the_gaps_the_youtube_track_does_not_cover() -> None:
    entries = render.parse_srt(SRT) + [render.SrtEntry("00:01:00,000", "00:01:02,000", "far away")]
    words = render.load_words({"segments": [{"words": WORDS}]})
    cues, kept = render.caption_source("both", YOUTUBE_TRACK, entries, words)
    assert [c.text for c in cues][0] == "Auntie's back again"
    assert "far away" in [c.text for c in cues]  # the whisper cue with no line stays
    assert kept == []


def test_a_whisper_only_run_keeps_whisper_even_with_prefer_youtube() -> None:
    entries = render.parse_srt(SRT)
    words = render.load_words({"segments": [{"words": WORDS}]})
    marker = {"video_id": "VID", "empty": True, "lines": [], "language_code": "en"}
    cues, kept = render.caption_source("youtube", marker, entries, words)
    assert [c.text for c in cues] == ["Aunt's back again"] and len(kept) == 3
