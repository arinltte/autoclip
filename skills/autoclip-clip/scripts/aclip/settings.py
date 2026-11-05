"""The ``settings.md`` parser — one module, typed access to every key.

``settings.md`` (the plain-markdown file the Setup Skill writes from the user's
answers, tracked in the repo root as the default) is the input of record for
every pipeline stage, so every key is parsed here once instead of each stage
re-deriving its own text rules.

File rules implemented:

* only ``key: value`` lines matter — headers (``#``), prose, and blank lines
  are skipped, and a key must be ``^[a-z][a-z0-9_]*$`` so prose with a colon
  in it never masquerades as a setting;
* the value is the text before the first em-dash ``—``; everything after it is
  the plain-language gloss (``model: — the Coding Agent's own model`` therefore
  reads as a *blank* value);
* typed values: ``none``/blank → ``None``, ``true``/``false`` → bool, integers
  stay ``int``, decimals ``float``, anything else stays a string;
* ``title_card``/``captions`` carry ``k=v, k=v`` lists which parse into dicts;
  a sub-value that is empty or an inline ``( … )`` gloss (``font= (empty =
  bundled default)``) is ``None``.

Location: ``<project root>/settings.md`` per the env contract
(:func:`aclip.env.paths`), re-read from the environment on every call so a test
or a long-running process sees the current value; ``AUTOCLIP_SETTINGS``
overrides the path (tests, advanced users). A missing file is a clear
``FileNotFoundError`` naming the full path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .env import paths

SETTINGS_FILENAME = "settings.md"
SETTINGS_ENV_OVERRIDE = "AUTOCLIP_SETTINGS"

# --- the frozen defaults (the tracked repo-root settings.md parses to these) ---

DEFAULT_MODEL: str | None = None
DEFAULT_LANGUAGE = "en"
DEFAULT_SIMILARITY_THRESHOLD = 0.85
DEFAULT_PARALLELISM = 1
DEFAULT_CHUNK_MINUTES = 6
DEFAULT_INTRO_SEC = 90
DEFAULT_CLIP_MIN_SEC = 30
DEFAULT_CLIP_MAX_SEC = 90
DEFAULT_TARGET_COUNT = 10
DEFAULT_MIN_SCORE: float | None = None
#: The playback-speed CAP, not a default: a clip that already fits the
#: preset's max_sec renders at 1.0, and a longer one takes the smallest ladder
#: step that fits (render.SPEED_LADDER). The old constant 1.3 applied itself to
#: every clip regardless of length.
DEFAULT_SPEED = 1.3
#: ADR 0007 / spec decision 7 — the compared-tracks ask (A whisper / B youtube /
#: C both), remembered in settings.md; whisper wording on whisper word timings.
DEFAULT_PREFER = "whisper"
PREFER_CHOICES = ("whisper", "youtube", "both")

DEFAULT_TITLE_CARD: dict[str, Any] = {
    "enabled": True,
    "duration_sec": 6,
    "fontsize": 46,
    "box": True,
    "box_opacity": 0.45,
    "font": None,  # empty = bundled default
    "fontcolor": "#ffffff",
    "kicker": "HIGHLIGHT",
    "kicker_color": "#ffd24a",
    # the wrap: at most max_lines lines, padding_pct of the frame kept clear of
    # text, the font shrinks to min_fontscale before a third line is used
    "max_lines": 2,
    "padding_pct": 6,
    "min_fontscale": 0.7,
    "max_chars": None,  # an explicit per-line cap; blank = derived from the padding
}

DEFAULT_CAPTIONS: dict[str, Any] = {
    "enabled": True,
    "style": "karaoke",
    "fontsize": 54,
    "spoken_color": "#ffff00",
    "unspoken_color": "#ffffff",
    "outline": 2,
    "margin_v": 420,
}

_NESTED_KEYS = ("title_card", "captions")


@dataclass(frozen=True)
class Settings:
    """The parsed settings, one typed attribute per known key.

    Absent keys fall back to the frozen defaults above, so a partial settings
    file (a tmp file with one ``model:`` override, say) still resolves to a
    complete run configuration. ``raw`` carries every parsed key, nested ones
    included, so later tickets can reach values this ticket does not model.
    """

    model: str | None = DEFAULT_MODEL
    language: str = DEFAULT_LANGUAGE
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD
    parallelism: int = DEFAULT_PARALLELISM
    chunk_minutes: float = DEFAULT_CHUNK_MINUTES
    intro_sec: float = DEFAULT_INTRO_SEC
    clip_min_sec: int = DEFAULT_CLIP_MIN_SEC
    clip_max_sec: int = DEFAULT_CLIP_MAX_SEC
    target_count: int = DEFAULT_TARGET_COUNT
    min_score: float | None = DEFAULT_MIN_SCORE
    speed: float = DEFAULT_SPEED
    #: which track's WORDING the captions use (ADR 0007): whisper (karaoke on
    #: whisper word timings), youtube or both (line-level — no word timings are
    #: ever invented for the YouTube Track)
    prefer: str = DEFAULT_PREFER
    title_card: Mapping[str, Any] = field(default_factory=lambda: dict(DEFAULT_TITLE_CARD))
    captions: Mapping[str, Any] = field(default_factory=lambda: dict(DEFAULT_CAPTIONS))
    #: every key parsed from the file (nested keys as dicts), unknown ones included
    raw: Mapping[str, Any] = field(default_factory=dict)
    #: the file this instance was loaded from (None when built by hand)
    path: Path | None = None

    @property
    def sequential(self) -> bool:
        """True when analysis runs one chunk at a time (the default)."""
        return self.parallelism == 1


def settings_path() -> Path:
    """Where settings.md lives right now: env override, else project root.

    Re-read per call (the env contract's override-then-default pattern): a test
    that exports ``AUTOCLIP_SETTINGS`` must not race an import-time snapshot.
    """
    override = os.environ.get(SETTINGS_ENV_OVERRIDE)
    if override:
        return Path(override).expanduser()
    return paths().project_root / SETTINGS_FILENAME


def _is_key(text: str) -> bool:
    """True for a settings key (lower snake_case) and not a prose fragment."""
    return bool(text) and text[0].isalpha() and all(c.islower() or c.isdigit() or c == "_" for c in text)


def _strip_gloss(value: str) -> str:
    """Drop the trailing `` — …`` gloss, keeping the value text.

    Partitioning on the first em-dash covers both spellings: ``model: — the …``
    (value blank) and ``language: en — the …`` (value ``en``). No legal value in
    this file contains an em-dash, so the first one always ends the value.
    """
    return value.partition("—")[0].strip()


def _coerce(text: str) -> Any:
    """Type a raw value: ``none``/blank → None, bools, ints, floats, else str."""
    stripped = text.strip()
    if stripped == "" or stripped.lower() == "none":
        return None
    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        pass
    return stripped


def _coerce_sub_value(text: str) -> Any:
    """Type one ``k=v`` sub-value; an inline ``( … )`` gloss reads as blank."""
    stripped = text.strip()
    if stripped == "" or stripped.startswith("("):
        return None
    return _coerce(stripped)


def _parse_pairs(text: str) -> dict[str, str]:
    """Every ``key: value`` line in the file, as raw (gloss-stripped) strings."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, rest = stripped.partition(":")
        key = key.strip()
        if not _is_key(key):
            continue
        found[key] = _strip_gloss(rest)
    return found


def _parse_nested(value_text: str) -> dict[str, Any]:
    """Parse a ``k=v, k=v, …`` list (title_card, captions) into a dict."""
    parsed: dict[str, Any] = {}
    for piece in value_text.split(","):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        key, _, value = piece.partition("=")
        key = key.strip()
        if key:
            parsed[key] = _coerce_sub_value(value)
    return parsed


def _merge_over(base: dict[str, Any], parsed: Mapping[str, Any]) -> dict[str, Any]:
    """Parsed sub-keys override the defaults; unmentioned keys keep theirs."""
    merged = dict(base)
    merged.update(parsed)
    return merged


def _typed_prefer(value: Any) -> str:
    """Normalise a ``prefer:`` value to one of the three legal choices.

    Case-insensitive; anything else is a loud defect rather than a silent
    fallback — the compare step asks the user exactly this question and records
    the answer here, so an unreadable answer must not look like a legal one.
    """
    text = str(value).strip().lower()
    if text in PREFER_CHOICES:
        return text
    raise ValueError(
        f"prefer: {value!r} is not one of {', '.join(PREFER_CHOICES)} "
        "(the compare step's A whisper / B youtube / C both answer)"
    )


def settings_from_text(text: str, *, path: Path | None = None) -> Settings:
    """Parse settings.md text into a Settings instance (pure)."""
    pairs = _parse_pairs(text)
    raw: dict[str, Any] = {}
    for key, value_text in pairs.items():
        if key in _NESTED_KEYS:
            raw[key] = _parse_nested(value_text)
        else:
            raw[key] = _coerce(value_text)

    known = {
        key: value
        for key, value in raw.items()
        if key not in _NESTED_KEYS and key in {
            "model", "language", "similarity_threshold", "parallelism",
            "chunk_minutes", "intro_sec", "clip_min_sec", "clip_max_sec",
            "target_count", "min_score", "speed", "prefer",
        }
    }

    def typed(key: str, default: Any) -> Any:
        return known.get(key, default)

    return Settings(
        model=typed("model", DEFAULT_MODEL),
        language=typed("language", DEFAULT_LANGUAGE),
        similarity_threshold=typed("similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD),
        parallelism=typed("parallelism", DEFAULT_PARALLELISM),
        chunk_minutes=typed("chunk_minutes", DEFAULT_CHUNK_MINUTES),
        intro_sec=typed("intro_sec", DEFAULT_INTRO_SEC),
        clip_min_sec=typed("clip_min_sec", DEFAULT_CLIP_MIN_SEC),
        clip_max_sec=typed("clip_max_sec", DEFAULT_CLIP_MAX_SEC),
        target_count=typed("target_count", DEFAULT_TARGET_COUNT),
        min_score=typed("min_score", DEFAULT_MIN_SCORE),
        speed=typed("speed", DEFAULT_SPEED),
        prefer=_typed_prefer(typed("prefer", DEFAULT_PREFER)),
        title_card=_merge_over(dict(DEFAULT_TITLE_CARD), raw.get("title_card", {})),
        captions=_merge_over(dict(DEFAULT_CAPTIONS), raw.get("captions", {})),
        raw=raw,
        path=path,
    )


def load_settings(path: Path | str | None = None) -> Settings:
    """Load settings from ``path``, ``$AUTOCLIP_SETTINGS``, or the project root.

    A missing file raises ``FileNotFoundError`` naming the exact path looked
    for — the caller's fix is either to write the file or set the override.
    """
    if path is None:
        path = settings_path()
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"settings file not found: {path}")
    return settings_from_text(path.read_text(encoding="utf-8"), path=path)
