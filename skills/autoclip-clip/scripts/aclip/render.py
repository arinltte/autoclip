"""``aclip render`` — cues, animated captions, title card, speed (ticket #29).

Ported from the app era's render behaviour: the cut+concat originals
(``engine/backend/utils/video_processor.py`` — including the PATCH-5
apostrophe-safe concat rule) and the Rust render spec (``src/render.rs`` /
``src/cut.rs`` / ``src/fonts.rs`` / ``src/ffmpeg.rs``, the behaviour only —
all deleted at #37). One clip = one ffmpeg subprocess with a filter graph:

    split → blurred 9:16 background + centred foreground (the shorts preset,
    1080x1920, crop) → setpts (speed) → the karaoke ASS captions → the
    animated title card (fade-in + slide-up + fade-out, kicker above).

Rendering is the one parallel stage: plain ffmpeg subprocesses, no API, no
limit — the concurrency is a thread pool over the per-clip plans.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from aclip import cues as cues_module
from aclip import transcript_fetch
from aclip.cues import Cue, py_round
from aclip.env import child_env, paths

# --- the preset -----------------------------------------------------------------

# v1 makes YouTube Shorts. The app's preset map is ported for reference; the
# geometry the clip skill renders with is SHORTS (1080x1920, crop layout). The
# app's 60 s max_sec is NOT ported as a curation bound (curation owns the clip
# bounds in v1) — it IS ported as what the per-clip playback speed chases.
PRESETS: dict[str, dict[str, Any]] = {
    "douyin": {"label": "抖音 9:16", "w": 1080, "h": 1920, "layout": "blur", "max_sec": None},
    "xiaohongshu": {"label": "小红书 9:16", "w": 1080, "h": 1920, "layout": "blur", "max_sec": None},
    "shorts": {"label": "YouTube Shorts", "w": 1080, "h": 1920, "layout": "crop", "max_sec": 60},
    "bilibili": {"label": "B 站横屏", "w": 1920, "h": 1080, "layout": "fit", "max_sec": None},
    "original": {"label": "原画重编码", "w": None, "h": None, "layout": "none", "max_sec": None},
}
SHORTS_PRESET = "shorts"

#: The playback-speed ladder. ``settings.md``'s ``speed`` is the CAP (the old
#: constant default is retired): a clip that already fits the preset's
#: ``max_sec`` renders at 1.0 and a longer one takes the smallest ladder step
#: that fits it, never more. The one-speed-fits-all run is what produced ten
#: clips all pegged to the ceiling — nine of them unchanged content-wise.
SPEED_LADDER: tuple[float, ...] = (1.0, 1.1, 1.2, 1.25, 1.3, 1.4, 1.5, 1.75, 2.0)


def clamp_speed(value: Any) -> float:
    """``settings.speed`` as a legal factor: the ceiling, in [1.0, 2.0].

    Below 1.0 (a slow-motion clip) is not a legal setting — the floor is 1.0,
    which is also "no change". 2.0 is ``atempo``'s usable ceiling.
    """
    try:
        return min(max(float(value), 1.0), 2.0)
    except (TypeError, ValueError):
        return 1.0


def clip_speed(duration_sec: float, cap: float, target: float | None) -> float:
    """The playback speed ONE clip renders at.

    ``1.0`` when the clip already fits ``target`` (the preset's ``max_sec``) or
    when the preset sets no target at all; otherwise the smallest ladder step
    that brings it under the target, and ``cap`` (settings ``speed``) when even
    the cap will not — a clip longer than the platform allows still renders at
    the cap rather than being mangled further.
    """
    duration_sec = max(float(duration_sec), 0.0)
    if duration_sec <= 0.0 or not target or float(target) <= 0.0:
        return 1.0
    if duration_sec <= float(target):
        return 1.0
    for step in SPEED_LADDER:
        if step >= cap:
            return cap
        if duration_sec / step <= float(target):
            return step
    return cap

# --- the colour helpers ----------------------------------------------------------


def hex_to_ffmpeg(h: str) -> str:
    """``'#FFFFFF'`` → ``0xFFFFFF`` (drawtext fontcolor)."""
    return f"0x{h.lstrip('#').upper()}"


def hex_to_ass(h: str) -> str:
    """``'#FFFF00'`` → ``&H0000FFFF`` (ASS is ``&H00BBGGRR`` — BGR order)."""
    hexed = h.lstrip("#")
    red, green, blue = hexed[0:2], hexed[2:4], hexed[4:6]
    return f"&H00{blue.upper()}{green.upper()}{red.upper()}"


def escape_filter_path(path: Path) -> str:
    """publish_export._escape_filter_path: resolve, ``\\\\``→``/``, ``:``→``\\:``,
    ``'``→``\\'`` (the filter graph's quoting)."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    return str(resolved).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


# --- fonts ------------------------------------------------------------------------

CJK_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyh.ttf",
]

FONT_FILES: list[tuple[str, list[str]]] = [
    ("Helvetica", ["/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial.ttf"]),
    ("Arial", ["/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"]),
    ("PingFang SC", ["/System/Library/Fonts/PingFang.ttc"]),
    ("Avenir", ["/System/Library/Fonts/Avenir.ttc", "/Library/Fonts/Avenir.ttc"]),
    ("Georgia", ["/System/Library/Fonts/Supplemental/Georgia.ttf", "/Library/Fonts/Georgia.ttf"]),
    ("Times New Roman", ["/System/Library/Fonts/Supplemental/Times New Roman.ttf"]),
]


def resolve_cjk_font() -> Path | None:
    """The first CJK candidate font that exists (None → the title card is
    skipped with a warning, exactly like the engine's exporter)."""
    for candidate in CJK_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return Path(candidate)
    return None


def resolve_font_file(family: str) -> Path | None:
    """The family's first existing candidate; empty/unknown never resolves."""
    name = family.strip()
    if not name:
        return None
    for family_name, candidates in FONT_FILES:
        if family_name == name:
            for candidate in candidates:
                expanded = candidate.replace("~", str(Path.home()), 1)
                if Path(expanded).is_file():
                    return Path(expanded)
            return None
    return None


# --- the animated title card ------------------------------------------------------


@dataclass(frozen=True)
class TitleExprs:
    alpha: str
    y: str
    enable: str
    kicker_alpha: str


def title_exprs(title: Mapping[str, Any]) -> TitleExprs:
    """drawtext expressions for the animated title card — fade in 0.5 s → hold →
    fade out 0.6 s. The ``\\,`` comma escapes are load-bearing: the expressions
    live inside a ``-filter_complex`` argument."""
    duration = float(title.get("duration_sec", 4.2))
    hold = max(duration - 0.6, 0.0)
    esc = "\\,"
    return TitleExprs(
        alpha=(
            f"if(lt(t{esc}0.5){esc}t/0.5{esc}if(lt(t{esc}{hold:.2f}){esc}1"
            f"{esc}max(0{esc}({duration:.2f}-t)/0.6)))"
        ),
        y=f"h*0.08+40*(1-min(1{esc}t/0.7))",
        enable=f"lt(t{esc}{duration + 0.2:.2f})",
        kicker_alpha=(
            f"if(lt(t{esc}0.7){esc}0{esc}if(lt(t{esc}1.2){esc}(t-0.7)/0.5{esc}"
            f"if(lt(t{esc}{hold:.2f}){esc}1{esc}max(0{esc}({duration:.2f}-t)/0.6))))"
        ),
    )


#: the widest glyph-run a title line may hold, as a fraction of the font size.
#: Calibrated against the fonts this project's box actually resolves for a
#: blank `font:` (STHeiti Light, Arial Unicode, Helvetica) at fontsize 46:
#: ordinary prose runs ~0.48, an all-caps or wide-glyph line ~0.60. 0.62 keeps
#: headroom for a shouty line and still wraps a normal sentence in two.
MAX_GLYPH_RATIO = 0.62
DEFAULT_PADDING_PCT = 6
DEFAULT_MAX_LINES = 2
DEFAULT_MIN_FONT_SCALE = 0.7


def budget_chars(
    frame_width: int | float,
    fontsize: int | float,
    padding_pct: float = DEFAULT_PADDING_PCT,
    max_chars: int | None = None,
) -> int:
    """How many characters one title line may hold inside the frame's padding.

    ``max_chars`` (a settings override) wins outright; otherwise the budget is
    the usable width divided by ``fontsize * MAX_GLYPH_RATIO``. The floor of 8
    keeps a pathological ``padding_pct`` from producing a zero-width budget.
    """
    if max_chars:
        return max(int(max_chars), 8)
    padding = float(frame_width) * float(padding_pct) / 100.0
    usable = float(frame_width) - 2.0 * padding
    return max(int(usable / (float(fontsize) * MAX_GLYPH_RATIO)), 8)


def wrap_title(title: str, budget: int) -> list[str]:
    """Word-wrap a title into lines of at most ``budget`` characters.

    drawtext draws ONE line: it never wraps, and an over-wide line is simply
    clipped off both edges of the frame (a 55-character title at fontsize 46
    measured 1202 px wide on a 1080 frame in the real run). So the wrap is done
    here, in Python, before the textfile is written. A single word longer than
    the budget is hard-split — a character is never dropped and no line ever
    runs past the frame. Runs of whitespace collapse; an empty title yields no
    lines.
    """
    budget = max(int(budget), 1)
    lines: list[str] = []
    current = ""
    for word in " ".join(str(title).split()).split(" "):
        if not word:
            continue
        while len(word) > budget:  # one unbreakable word: hard-split it
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:budget])
            word = word[budget:]
        candidate = f"{current} {word}" if current else word
        if len(candidate) > budget and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def fit_title(
    title: str,
    frame_width: int | float,
    fontsize: int | float,
    padding_pct: float = DEFAULT_PADDING_PCT,
    max_lines: int = DEFAULT_MAX_LINES,
    min_fontscale: float = DEFAULT_MIN_FONT_SCALE,
    max_chars: int | None = None,
) -> tuple[str, int]:
    """``(textfile body, fontsize)`` — the title as it should be drawn.

    The font shrinks in steps of 2 until the wrapped title fits
    ``max_lines``; the floor is ``min_fontscale`` of the configured size. A
    title that still will not fit at the floor keeps every line at that size:
    it wraps to a third line rather than being truncated or overflowing.
    """
    base = max(int(fontsize or 46), 8)
    lines_allowed = max(int(max_lines or DEFAULT_MAX_LINES), 1)
    floor = max(round(base * max(float(min_fontscale or 0.0), 0.1)), 8)
    size = base
    while size > floor:
        if len(wrap_title(title, budget_chars(frame_width, size, padding_pct, max_chars))) <= lines_allowed:
            break
        size -= 2
    body = "\n".join(
        wrap_title(title, budget_chars(frame_width, size, padding_pct, max_chars))
    )
    return body, size


def fit_title_settings(
    title: str, frame_width: int | float, title_settings: Mapping[str, Any]
) -> tuple[str, int]:
    """``fit_title`` against the ``title_card`` settings mapping, its usual input."""
    t = dict(title_settings or {})
    return fit_title(
        title,
        frame_width,
        t.get("fontsize", 46),
        padding_pct=t.get("padding_pct", DEFAULT_PADDING_PCT),
        max_lines=t.get("max_lines", DEFAULT_MAX_LINES),
        min_fontscale=t.get("min_fontscale", DEFAULT_MIN_FONT_SCALE),
        max_chars=t.get("max_chars"),
    )


@dataclass(frozen=True)
class TitleChain:
    parts: list[str]
    out_label: str
    title_file: Path
    title_body: str
    fontsize: int = 0


def build_title_chain(
    in_label: str,
    title: str,
    font_path: Path | None,
    settings: Mapping[str, Any],
    title_file: Path,
    frame_width: int | float = 1080,
) -> TitleChain:
    """The animated title card as labelled drawtext filters, chained
    ``in_label`` → ``tout`` through TWO labelled drawtexts (unlabelled chained
    drawtexts fail). No kicker → the main title relabels to ``tout``.

    ``frame_width`` is what the wrap is measured against (the preset's width):
    a title is wrapped to ``max_lines`` lines with ``padding_pct`` of the frame
    kept clear, and the font shrinks to make that fit. """

    def number(value: Any, default: Any) -> str:
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return repr(value)
        return str(default)

    t = dict(settings.get("title", {}))
    body, fontsize = fit_title_settings(title, frame_width, t)
    exprs = title_exprs(t)
    esc_file = escape_filter_path(title_file)
    font_arg = (
        f"fontfile='{escape_filter_path(font_path)}':" if font_path is not None else ""
    )
    fontcolor = hex_to_ffmpeg(t.get("fontcolor", "#FFFFFF"))
    box_args = ""
    if t.get("box", True):
        box_args = f"box=1:boxcolor=black@{number(t.get('box_opacity', 0.45), 0.45)}:boxborderw=18"
    fontsize_arg = str(fontsize)
    # expansion=none: titles may contain '%' — drawtext's default expansion
    # treats a bare % as a %{…} sequence and fails with "Stray %".
    main = (
        f"[{in_label}]drawtext={font_arg}textfile='{esc_file}':reload=0:x=(w-text_w)/2:"
        f"y='{exprs.y}':fontsize={fontsize_arg}:fontcolor={fontcolor}:borderw=3:bordercolor=black:"
        f"{box_args}:alpha='{exprs.alpha}':enable='{exprs.enable}':expansion=none[t1]"
    )
    parts = [main]
    out_label = "tout"
    kicker = t.get("kicker", "")
    if kicker:
        kicker_font = (
            f"fontfile='{escape_filter_path(font_path)}':" if font_path is not None else ""
        )
        kicker_color = hex_to_ffmpeg(t.get("kicker_color", "#FFD24A"))
        parts.append(
            f"[t1]drawtext={kicker_font}text='{kicker}':reload=0:x=(w-text_w)/2:y='h*0.08-52':"
            f"fontsize=30:fontcolor={kicker_color}:borderw=2:bordercolor=black:"
            f"alpha='{exprs.kicker_alpha}':enable='{exprs.enable}':expansion=none[{out_label}]"
        )
    else:
        # relabel the main title's output to close the chain
        parts[0] = parts[0].replace("[t1]", "[tout]")
    return TitleChain(parts, out_label, title_file, body, fontsize)


# --- the karaoke captions (ASS) ---------------------------------------------------


ASS_HEADER = (
    "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
    "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
    "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,"
    "Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
)


def ass_style_line(settings: Mapping[str, Any]) -> str:
    """The Cap style — karaoke colours (Secondary = unspoken, Primary = spoken)."""
    cap = settings.get("caption", {})
    primary = hex_to_ass(cap.get("spoken_color", "#FFFF00"))
    secondary = hex_to_ass(cap.get("unspoken_color", "#FFFFFF"))
    fontsize = cap.get("fontsize", 54)
    outline = cap.get("outline", 2)
    margin_v = cap.get("margin_v", 420)
    return (
        f"Style: Cap,Helvetica,{fontsize},{primary},{secondary},&H00000000,&H00000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},1,2,60,60,{margin_v},1\n"
    )


def fmt_ass_time(t: float) -> str:
    """Centiseconds, ``int(round(t*100))`` (ties to even), hours unpadded."""
    cs = py_round(t * 100.0)
    hours, cs = divmod(cs, 360000)
    minutes, cs = divmod(cs, 6000)
    seconds, cs = divmod(cs, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{cs:02}"


_SRT_TIMESTAMP = re.compile(r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})")


@dataclass(frozen=True)
class SrtEntry:
    start_time: str
    end_time: str
    text: str


def parse_srt(text: str) -> list[SrtEntry]:
    """Parse an SRT document into cue rows: the timestamp is the block's SECOND
    line (regex-anchored at the line start); anything else is skipped, never an
    error."""
    out: list[SrtEntry] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        match = _SRT_TIMESTAMP.match(lines[1])
        if match is None:
            continue
        out.append(
            SrtEntry(
                start_time=match.group(1).replace(".", ","),
                end_time=match.group(2).replace(".", ","),
                text=" ".join(lines[2:]),
            )
        )
    return out


@dataclass(frozen=True)
class Word:
    word: str
    start: float
    end: float


def load_words(payload: Mapping[str, Any]) -> list[Word]:
    """Flatten a parsed words.json: every non-empty, stripped word of every
    segment, in order."""
    out: list[Word] = []
    for segment in payload.get("segments", []):
        for word in segment.get("words", []):
            text = str(word.get("word", "")).strip()
            if not text:
                continue
            out.append(Word(text, float(word.get("start", 0.0)), float(word.get("end", 0.0))))
    return out


def to_seconds(t: str) -> float:
    """``HH:MM:SS,mmm`` (or ``MM:SS``) → seconds. A non-time is a ValueError."""
    normalized = t.strip().replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, minutes, seconds = "0", parts[0], parts[1]
    else:
        raise ValueError(f"invalid time: {t!r}")
    try:
        return float(hours) * 3600.0 + float(minutes) * 60.0 + float(seconds)
    except ValueError as exc:
        raise ValueError(f"invalid time: {t!r}") from exc


def _srt_time(seconds: float) -> str:
    """Seconds → the ``HH:MM:SS,mmm`` spelling parse_srt reads back."""
    total_ms = py_round(seconds * 1000.0)
    hours, total_ms = divmod(total_ms, 3600000)
    minutes, total_ms = divmod(total_ms, 60000)
    secs, ms = divmod(total_ms, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}.{ms:03}".replace(".", ",")


def youtube_cues(lines: Sequence[Mapping[str, Any]]) -> list[SrtEntry]:
    """The YouTube Track's lines as line-level cues (ADR 0007).

    Each line's own start/duration is the cue's span; the wording is the site's.
    No word timings exist for it and none are ever invented — build_ass sees an
    empty word list and degrades karaoke to the whole-line highlight.
    """
    out: list[SrtEntry] = []
    for line in lines:
        text = str(line.get("text", "")).strip()
        try:
            start = float(line.get("start", 0.0))
            duration = float(line.get("duration", 0.0))
        except (TypeError, ValueError):
            continue
        if not text or duration <= 0.0:
            continue
        out.append(SrtEntry(start_time=_srt_time(start), end_time=_srt_time(start + duration), text=text))
    return out


def caption_source(
    prefer: str,
    youtube_track: Mapping[str, Any] | None,
    srt_entries: Sequence[SrtEntry],
    words: Sequence[Word],
) -> tuple[Sequence[SrtEntry], Sequence[Word]]:
    """The caption wording + timings for the run's ``prefer:`` (ADR 0007).

    ``whisper`` (the default): the Whisper Track — its cues with word timings,
    so karaoke highlights word by word. ``youtube``/``both``: the YouTube Track's
    wording on its own line timings with NO word timings (never invented), so the
    captions render whole-line. ``both`` also keeps the whisper cues the YouTube
    track has nothing to say over, so a sparse caption track still captions.
    """
    if prefer not in {"youtube", "both"} or youtube_track is None or youtube_track.get("empty"):
        return list(srt_entries), list(words)
    lines = youtube_track.get("lines") or []
    youtube = youtube_cues(lines)
    if prefer == "youtube":
        return youtube, []
    covered = [(to_seconds(cue.start_time), to_seconds(cue.end_time)) for cue in youtube]
    filler = [
        cue
        for cue in srt_entries
        if not any(start < end_cue and start_cue < end for start, end in covered
                   for start_cue, end_cue in [(to_seconds(cue.start_time), to_seconds(cue.end_time))])
    ]
    merged = sorted([*youtube, *filler], key=lambda cue: to_seconds(cue.start_time))
    return merged, []


def build_ass(
    cues: Sequence[SrtEntry],
    words: Sequence[Word],
    clip_start: float,
    clip_end: float,
    settings: Mapping[str, Any],
    speed: float,
) -> str:
    """The ASS document for one clip.

    Cues overlapping ``[clip_start, clip_end)`` shift to clip-local time and
    divide by ``speed`` (the video is sped up BEFORE the text filters, so the
    text timeline is the OUTPUT timeline). Karaoke style + in-cue word timings
    → per-word ``{\\k<centisec>}`` fills (duration to the NEXT word's start, ÷
    speed, min 1 cs; the trailing space belongs to the highlighted span — without
    it ASS renders words concatenated). Every dialogue line carries the
    fade/pop-in prefix.
    """
    cap = settings.get("caption", {})
    cap_style = cap.get("style", "karaoke")
    out = [ASS_HEADER, ass_style_line(settings)]
    out.append("\n[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    for cue in cues:
        try:
            start = to_seconds(cue.start_time)
            end = to_seconds(cue.end_time)
        except ValueError:
            continue
        if end <= clip_start or start >= clip_end:
            continue
        text = cue.text.strip()
        if not text:
            continue
        line_start = max(start - clip_start, 0.0) / speed
        line_end = (min(clip_end, end) - clip_start) / speed
        cue_words = [w for w in words if w.start < end - 0.01 and w.end > start + 0.01]
        if cue_words and cap_style == "karaoke":
            parts = []
            for index, word in enumerate(cue_words):
                nxt = cue_words[index + 1].start if index + 1 < len(cue_words) else end
                kcs = max(py_round((nxt - word.start) / speed * 100.0), 1)
                parts.append(f"{{\\k{kcs}}}{word.word} ")
            line_text = "".join(parts)
        else:
            line_text = text
        out.append(
            f"Dialogue: 0,{fmt_ass_time(line_start)},{fmt_ass_time(line_end)},Cap,,0,0,0,,"
            f"{{\\fad(120,150)\\t(0,120,\\fscx105\\fscy105)\\t(120,240,\\fscx100\\fscy100)}}{line_text}\n"
        )
    return "".join(out)


# --- the render plan and the command ------------------------------------------------


@dataclass
class RenderPlan:
    speed: float
    start: float
    duration: float
    duration_sec: float
    title: str
    ass_body: str | None
    title_chain: TitleChain | None
    args: list[str] = field(default_factory=list)
    # The REAL paths the filter graph references — render_one writes the bodies
    # exactly here (a second temp dir would break the graph's file reads).
    ass_file: Path | None = None
    title_file: Path | None = None


def clip_title(clip: Mapping[str, Any]) -> str:
    """generated_title → title → outline → ``clip<id>`` — empty strings fall
    through."""
    for key in ("generated_title", "title", "outline"):
        value = clip.get(key)
        if isinstance(value, str) and value:
            return value
    return f"clip{clip.get('id', '?')}"


def slug(text: str) -> str:
    """Keep alnum + ``-_ `` (unicode alnum), collapse whitespace runs to ``_``,
    cap at 80 characters, never empty. Every other character becomes one ``_``
    of its own (the app-era rule, ported as-is), so ``a $300k — x`` lands as
    ``a__300k___x`` — wordy, but faithful and never ambiguous."""
    kept = "".join(c if c.isalnum() or c in "-_ " else "_" for c in text)
    joined = "_".join(kept.split())
    out = joined[:80]
    return out or "clip"


def _py_round2(value: float) -> float:
    """Python's ``round(v, 2)``: format-then-parse (the app-era py_round2 rule)."""
    return float(f"{value:.2f}")


def plan_render(
    clip_start: float,
    clip_end: float,
    title: str,
    settings: Mapping[str, Any],
    srt_entries: Sequence[SrtEntry],
    words: Sequence[Word],
    cjk_font: Path | None,
    video: Path,
    out_path: Path,
    tmp_dir: Path,
    preset: Mapping[str, Any] | None = None,
) -> RenderPlan:
    """The planning half for one clip: the filter graph and the ffmpeg argv
    (``tmp_dir`` hosts ``cap.ass`` / ``title.txt``; the driver writes the
    bodies, runs ffmpeg, then removes them)."""
    preset = preset if preset is not None else PRESETS[SHORTS_PRESET]
    duration = max(clip_end - clip_start, 0.1)
    # speed is per clip: the cap is the settings value, the clip's own length
    # decides how much of it this clip uses
    speed = clip_speed(duration, clamp_speed(settings.get("speed", 1.0)), preset.get("max_sec"))
    width = preset.get("w") or 1080
    height = preset.get("h") or 1920

    # 1) the 9:16 layout: blurred background + centred foreground
    parts = [
        "[0:v]split=2[bg][fg]",
        f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},gblur=sigma=24[bg2]",
        f"[fg]scale={width}:-2[fg2]",
        "[bg2][fg2]overlay=(W-w)/2:(H-h)/2[base]",
    ]
    # 2) playback speed — setpts compresses video time BEFORE the text filters
    last = "base"
    if speed != 1.0:
        parts.append(f"[{last}]setpts=PTS/{speed}[spd]")
        last = "spd"
    # 3) the karaoke captions (ASS); only when there is at least one Dialogue
    ass_body: str | None = None
    if settings.get("caption", {}).get("enabled", True):
        ass = build_ass(srt_entries, words, clip_start, clip_start + duration, settings, speed)
        if ass.count("Dialogue") > 0:
            ass_file = tmp_dir / "cap.ass"
            parts.append(f"[{last}]ass='{escape_filter_path(ass_file)}'[sub]")
            last = "sub"
            ass_body = ass
    # 4) the animated title card — the chosen family's file, else the CJK fallback
    title_chain: TitleChain | None = None
    if settings.get("title", {}).get("enabled", True):
        family = settings.get("title", {}).get("font", "")
        title_font = (resolve_font_file(family) if family else None) or (
            cjk_font if cjk_font is not None else None
        )
        chain = build_title_chain(
            last, title, title_font, settings, tmp_dir / "title.txt", width
        )
        parts.extend(chain.parts)
        last = chain.out_label
        title_chain = chain

    args = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{clip_start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(video),
        "-filter_complex",
        ";".join(parts),
        "-map",
        f"[{last}]",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
    ]
    # atempo splices in at the -c:a index (audio-only filter, pitch preserved)
    if speed != 1.0:
        args += ["-af", f"atempo={speed}"]
    args += [
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        "-y",
        str(out_path),
    ]
    return RenderPlan(
        speed=speed,
        start=clip_start,
        duration=duration,
        duration_sec=_py_round2(duration / speed),
        title=title,
        ass_body=ass_body,
        title_chain=title_chain,
        args=args,
        ass_file=(tmp_dir / "cap.ass") if ass_body is not None else None,
        title_file=title_chain.title_file if title_chain is not None else None,
    )


def compile_args(concat_file: Path, out_path: Path) -> list[str]:
    """``concat_clips``' compilation command (identical-codec stream copy)."""
    return [
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-y",
        str(out_path),
    ]


def concat_list_lines(paths: Sequence[Path]) -> list[str]:
    """The concat demuxer's file lines with the PATCH-5 apostrophe rule:
    the demuxer's quoting is ``'\\''`` (shell style) — ``"'"'"'`` fails to parse,
    so a title with an apostrophe (Aunt's) killed collections. Every path is
    absolute, every ``'`` escaped as ``'\\''``. (The regression is re-pinned by
    tests.)"""
    lines = []
    for path in paths:
        escaped = str(Path(path).absolute()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    return lines


# --- the driver --------------------------------------------------------------------

Runner = Callable[[list[str], dict[str, str]], Any]


def ffmpeg_path() -> Path:
    """The static project ffmpeg from the env contract (drawtext+libass)."""
    return paths().ffmpeg_bin


def default_runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — the fixed argv; shell=False
        [str(ffmpeg_path()), *argv],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )


def render_one(
    plan: RenderPlan,
    runner: Runner | None = None,
    keep_tmp: bool = False,
) -> dict[str, Any]:
    """Write the ASS/title bodies the graph references, run ffmpeg, clean up.

    The bodies go to the plan's OWN paths (ass_file / title_file) — the filter
    graph already points at them; a different temp dir would starve libass.
    Returns a result row.
    """
    runner = runner or default_runner
    written: list[Path] = []
    try:
        if plan.ass_body is not None and plan.ass_file is not None:
            plan.ass_file.parent.mkdir(parents=True, exist_ok=True)
            plan.ass_file.write_text(plan.ass_body, encoding="utf-8")
            written.append(plan.ass_file)
        if plan.title_chain is not None:
            title_file = plan.title_file or plan.title_chain.title_file
            title_file.parent.mkdir(parents=True, exist_ok=True)
            title_file.write_text(plan.title_chain.title_body, encoding="utf-8")
            written.append(title_file)
        proc = runner(plan.args, child_env())
        out_path = plan.args[-1]
        ok = getattr(proc, "returncode", 0) == 0 and Path(out_path).is_file()
        return {
            "ok": ok,
            "title": plan.title,
            "start": plan.start,
            "speed": plan.speed,
            "duration_sec": plan.duration_sec,
            "out_path": out_path,
            "stderr": (getattr(proc, "stdout", "") or "")[-2000:],
        }
    finally:
        if not keep_tmp:
            for path in written:
                path.unlink(missing_ok=True)
            # the per-clip mkdtemp the caller handed the plan: drop it when empty
            if plan.ass_file is not None and plan.ass_file.parent.name.startswith("aclip-render-"):
                try:
                    plan.ass_file.parent.rmdir()
                except OSError:
                    pass


def render_all(
    plans: Sequence[RenderPlan],
    runner: Runner | None = None,
    max_workers: int | None = None,
) -> list[dict[str, Any]]:
    """The one parallel stage: every clip's ffmpeg runs concurrently
    (plain subprocesses, no API, no limit). Returns the rows in plan order.

    The default worker count is the machine's CPU count — local ffmpeg is
    CPU-bound, so the cap protects the box, not an API quota (nothing here
    calls out; ``max_workers`` overrides it when a caller knows better).
    """
    runner = runner or default_runner
    if max_workers is None:
        max_workers = min(len(plans), os.cpu_count() or 4) or 1
    if not plans:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(render_one, plan, runner) for plan in plans]
        return [future.result() for future in futures]


# --- the CLI over a selection --------------------------------------------------------


def selection_path(project_dir: Path | str) -> Path:
    """The deterministic curation output the clip skill writes (see #31): the
    ordered Candidates the render renders."""
    return Path(project_dir) / "analyze" / "selection.json"


def load_selection(project_dir: Path | str) -> list[dict[str, Any]]:
    path = selection_path(project_dir)
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} is missing — the clip skill writes the curation selection before rendering"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    clips = payload.get("clips", payload if isinstance(payload, list) else [])
    return list(clips)


def render_settings(loaded: Any) -> dict[str, Any]:
    """The render's expected settings shape from the parsed settings.md:
    ``speed`` at the root, the title card under ``title``, the captions under
    ``caption`` (the app era's settings.json geometry, unchanged)."""
    return {
        "speed": loaded.speed,
        "title": dict(loaded.title_card),
        "caption": dict(loaded.captions),
    }


def render_project(
    video_id: str,
    runner: Runner | None = None,
    settings: Any = None,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """Render the whole selection for one Project.

    The caption wording follows ``prefer:`` (ADR 0007): whisper by default,
    the YouTube Track's own lines — line-level, no invented word timings — when
    the user answered B or C to the compare step's question.
    """
    from aclip import settings as settings_module

    loaded = settings if settings is not None else settings_module.load_settings()
    resolved = render_settings(loaded)
    project_dir = paths().project_root / "projects" / video_id
    clips = load_selection(project_dir)
    media = None
    for candidate in sorted((project_dir / "download").glob("*")):
        if candidate.is_file() and candidate.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
            media = candidate
            break
    if media is None:
        raise FileNotFoundError(f"no media in {project_dir / 'download'} — run aclip download first")
    transcript_dir = project_dir / "transcript"
    srt_paths = sorted(transcript_dir.glob("*.srt"))
    words_paths = sorted(transcript_dir.glob("*.words.json"))
    if not srt_paths or not words_paths:
        raise FileNotFoundError(
            f"the Project needs both the SRT and the words.json tracks in {transcript_dir} — "
            "run aclip transcribe first"
        )
    srt_entries = parse_srt(srt_paths[0].read_text(encoding="utf-8"))
    words = load_words(json.loads(words_paths[0].read_text(encoding="utf-8")))
    # the wording the captions carry: prefer=youtube/both reads the YouTube Track
    # (ADR 0007 — line-level, word timings never invented)
    youtube_track_path = transcript_fetch.youtube_track_path(project_dir)
    youtube_track: dict[str, Any] | None = None
    if youtube_track_path.is_file():
        youtube_track = json.loads(youtube_track_path.read_text(encoding="utf-8"))
    srt_entries, words = caption_source(loaded.prefer, youtube_track, srt_entries, words)
    output_dir = project_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    cjk_font = resolve_cjk_font()

    plans: list[RenderPlan] = []
    for index, clip in enumerate(clips, start=1):
        start = float(clip["start"])
        end = float(clip["end"])
        # the clip's own rank is the filename prefix, so a continuation batch
        # (curate --skip 10) writes 11_, 12_, … and never overwrites 01_..10_
        rank = int(clip.get("rank") or index)
        out_name = f"{rank:02d}_{slug(clip_title(clip))}.mp4"
        plans.append(
            plan_render(
                start,
                end,
                clip_title(clip),
                resolved,
                srt_entries,
                words,
                cjk_font,
                media,
                output_dir / out_name,
                Path(tempfile.mkdtemp(prefix=f"aclip-render-{rank:02d}-")),
            )
        )
    rows = render_all(plans, runner=runner, max_workers=max_workers)
    return {"video_id": video_id, "clips": rows, "output_dir": str(output_dir)}


def main(argv: Sequence[str] | None = None) -> int:
    """``aclip render <Video Id>`` — renders the selection concurrently."""
    argv = list(sys.argv[2:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: aclip render <Video Id>", file=sys.stderr)
        return 0 if argv and argv[0] in {"-h", "--help"} else 2
    video_id = argv[0]
    try:
        result = render_project(video_id)
    except Exception as exc:  # noqa: BLE001 — a real defect, reported loudly
        print(f"render: {exc}", file=sys.stderr)
        return 1
    failed = 0
    for row in result["clips"]:
        if row["ok"]:
            print(
                f"rendered: {row['out_path']} "
                f"(speed {row['speed']:.2f}, {row['duration_sec']:.1f}s)"
            )
        else:
            failed += 1
            print(f"FAILED: {row['title']} — {row['stderr'][-400:]}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    sys.exit(main())
