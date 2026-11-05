"""The skill documents' structure (spec testing decisions): the three SKILL.md
files carry their required sections, and the setup interview names every
frozen ``settings.md`` key. Cheap — and it keeps the three skills in sync with
the settings file. #33 (clip) and #34 (help) extend the per-skill specs below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parents[3]  # skills/
REPO_ROOT = SKILLS_DIR.parent

SETTINGS_TOP_LEVEL_KEYS = {
    "model", "language", "similarity_threshold", "parallelism", "chunk_minutes",
    "intro_sec", "clip_min_sec", "clip_max_sec", "target_count", "min_score",
    "speed", "prefer", "title_card", "captions",
}
SETTINGS_NESTED_KEYS = {
    "enabled", "duration_sec", "fontsize", "box", "box_opacity", "font",
    "fontcolor", "kicker", "kicker_color", "style", "spoken_color",
    "unspoken_color", "outline", "margin_v",
    # the title card's wrap (#38): what keeps a long title off the frame's edge
    "max_lines", "padding_pct", "min_fontscale", "max_chars",
}

# The sections each SKILL.md must carry, per skill — all three asserted: setup
# from #32, the clip skill from #33, help from #34.
REQUIRED_SECTIONS = {
    "autoclip-setup": [
        "## The gate: `aclip doctor`",
        "## Step 1 — the settings interview (writes `settings.md`)",
        "## Step 2 — the environment (all idempotent; skip what exists)",
        "## Step 3 — verify",
    ],
    "autoclip-clip": [
        "## Before anything: the gate",
        "## The pipeline (in order)",
        "## The interrupt list — COMPLETE, all four, nothing else",
        "## Rules",
    ],
    "autoclip-help": [
        "## What this is",
        "## What you do",
        "## Where your clips land",
        "## Your settings, explained",
        "## When it will ask you something",
        "## If something looks wrong",
    ],
}


def _skill_text(name: str) -> str:
    path = SKILLS_DIR / name / "SKILL.md"
    assert path.is_file(), f"{name}/SKILL.md is missing"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("skill", list(REQUIRED_SECTIONS))
def test_required_sections_exist(skill: str) -> None:
    text = _skill_text(skill)
    for section in REQUIRED_SECTIONS[skill]:
        assert section in text, f"{skill}: missing section {section!r}"


def test_every_settings_key_is_named_by_the_setup_interview() -> None:
    """The interview must ask about every frozen settings.md key — the help
    skill's table is generated from the same file, so drift is caught here."""
    text = _skill_text("autoclip-setup")
    for key in sorted(SETTINGS_TOP_LEVEL_KEYS):
        assert key in text, f"autoclip-setup: the settings key {key!r} is not in the interview"
    for key in sorted(SETTINGS_NESTED_KEYS):
        assert f"`{key}`" in text, f"autoclip-setup: the nested key {key!r} is not in the interview"


def test_the_setup_skill_states_the_secrets_and_provider_rules() -> None:
    text = _skill_text("autoclip-setup")
    assert "never printed" in text and "never committed" in text
    assert "NVIDIA NIM is the only LLM provider" in text
    assert ".secrets/nvidia_api_key" in text


def test_the_clip_skill_names_every_mechanical_step() -> None:
    """Parent holds pointers, not payloads: every deterministic stage is an
    aclip subprocess or the documented module entry — never re-derived."""
    text = _skill_text("autoclip-clip")
    for command in (
        "python -m aclip scaffold",
        "python -m aclip download",
        "python -m aclip transcript-fetch",
        "python -m aclip transcribe",
        "python -m aclip compare",
        "python -m aclip.analysis plan",
        "python -m aclip.curate",
        "python -m aclip render",
    ):
        assert command in text, f"autoclip-clip: missing the step {command!r}"


def test_the_clip_interrupt_list_is_exactly_four() -> None:
    text = _skill_text("autoclip-clip")
    interrupt_section = text.split("## The interrupt list")[1]
    for phrase in (
        "Preflight failure",
        "Download exhausted",
        "zero-speech media",
        "Divergence below threshold",
        "Flagged spans over ~10% of the duration",
    ):
        assert phrase in interrupt_section, f"autoclip-clip: interrupt list missing {phrase!r}"
    # the pipeline step: the skill never uploads (the manual tool stays manual)
    assert "never uploads" in text
    # nothing else is interruptible: the list is introduced as COMPLETE
    assert "COMPLETE" in text


def test_the_clip_skill_pings_before_downloading() -> None:
    text = _skill_text("autoclip-clip")
    assert "max_tokens\": 512" in text
    assert "integrate.api.nvidia.com" in text
    assert "before the download" in text


def test_the_clip_preflight_retries_transient_statuses() -> None:
    """A gateway 504 stopped a real run that completed three minutes later: the
    preflight must classify, not blanket-stop. Fatal statuses (dead model or
    auth) stop immediately; 429/5xx retry the same ping, bounded."""
    text = _skill_text("autoclip-clip")
    preflight = text.split("**Preflight ping")[1].split("\n3. **Download**")[0]
    for status in ("429", "502", "503", "504"):
        assert status in preflight, f"autoclip-clip: the preflight does not name the transient status {status}"
    for status in ("401", "403", "404", "410"):
        assert status in preflight, f"autoclip-clip: the preflight does not name the fatal status {status}"
    assert "retry" in preflight.lower(), "autoclip-clip: the preflight has no retry rule"
    assert "sleep 60" in preflight, "autoclip-clip: the retry has no backoff"


def test_the_clip_skill_documents_the_full_analysis_and_its_continuation() -> None:
    """The three things a real run (#38) could not do: name the full analysis
    artifact, keep `target_count` as the render default, and continue past it."""
    text = _skill_text("autoclip-clip")
    assert "analyze/candidates.json" in text
    assert "target_count" in text
    assert "--skip" in text  # the continuation form: curate --skip 10, then render


def test_the_clip_skill_names_the_wrap_and_the_per_clip_speed() -> None:
    """The render contract the run's two cosmetic defects broke: the title
    wraps inside the frame's padding and the speed is per clip, not constant."""
    text = _skill_text("autoclip-clip")
    assert "max_lines" in text and "padding_pct" in text
    assert "per clip" in text or "per-clip" in text
    assert "1.0" in text  # a clip that fits renders unchanged


def test_the_clip_skill_records_the_report_array_instead_of_respawning() -> None:
    """The spawn defect (#38): the children printed their JSON instead of
    writing the answer file. The skill must say what to do with the array in
    the report — record it through the same gate, do not burn a re-spawn."""
    for name, needle in (
        ("autoclip-clip", "report"),
        ("autoclip-clip", "re-spawn"),
    ):
        assert needle in _skill_text(name), f"{name}: missing {needle!r}"
    spawn = (SKILLS_DIR / "autoclip-clip" / "references" / "analysis-spawn.md").read_text(encoding="utf-8")
    assert ".candidates.json" in spawn and "never" in spawn.lower()


def test_the_help_skill_reads_the_live_settings_and_marks_the_agent_keys() -> None:
    """Plain Language per ISO 24495-1:2023: the settings table reads the LIVE
    settings.md (never stale defaults) and marks model/parallelism agent-only."""
    text = _skill_text("autoclip-help")
    assert "live** `settings.md`" in text or "live `settings.md`" in text
    assert "The file always wins" in text
    for key in SETTINGS_TOP_LEVEL_KEYS:
        assert f"`{key}`" in text, f"autoclip-help: the settings table misses {key!r}"
    table = text.split("## Your settings, explained")[1]
    assert "**Agent-only**" in table
    for key in ("model", "parallelism"):
        row = next(line for line in table.splitlines() if line.startswith(f"| `{key}`"))
        assert "Agent-only" in row


SKILL_NAMES = ("autoclip-setup", "autoclip-clip", "autoclip-help")


def test_each_skill_carries_its_agent_descriptor() -> None:
    """agents/openai.yaml x3 with interface.display_name + short_description (#35)."""
    yaml = pytest.importorskip("yaml")
    for name in SKILL_NAMES:
        path = SKILLS_DIR / name / "agents" / "openai.yaml"
        assert path.is_file(), f"{name}/agents/openai.yaml is missing"
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        interface = payload.get("interface", {})
        assert interface.get("display_name"), f"{name}: no display_name"
        assert interface.get("short_description"), f"{name}: no short_description"


def test_the_readme_is_the_skill_sets_front_door() -> None:
    """The final README: logo header, pitch, pipeline diagram, setup-then-paste,
    folder map, upload-out-of-scope note, NVIDIA attribution (#35)."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert text.startswith("![AutoClip](logo.png)")
    assert "AutoClip is a folder of three coding-agent skills" in text
    assert "```" in text and "scaffold" in text and "render" in text  # the pipeline diagram
    assert "Run setup once, then paste a link" in text
    assert "projects/<Video Id>/output/" in text  # the folder map
    assert "Uploading is not part of the pipeline" in text
    assert "NVIDIA NIM" in text and "integrate.api.nvidia.com" in text
    # the audience is users of the skill set — the retired app's machinery is gone
    assert "Model Chain" not in text and "src/" not in text


def test_the_settings_keys_on_disk_match_the_tested_set() -> None:
    """Guard the guard: if settings.md grows a key, this test fails and the
    interview list must too."""
    tracked = REPO_ROOT / "settings.md"
    on_disk = set(re.findall(r"^([a-z][a-z0-9_]*):", tracked.read_text(encoding="utf-8"), re.M))
    assert on_disk == SETTINGS_TOP_LEVEL_KEYS


def _frontmatter(text: str) -> dict[str, str]:
    """The YAML-ish frontmatter of a SKILL.md as flat key/value pairs."""
    if not text.startswith("---\n"):
        return {}
    block = text.split("---\n", 2)[1]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip().strip('"')
    return out


@pytest.mark.parametrize("skill", ["autoclip-setup", "autoclip-clip", "autoclip-help"])
def test_every_skill_carries_discoverable_frontmatter(skill: str) -> None:
    """An agent harness loads a skill by its frontmatter: name + description.

    The first cut of the three skills had none — the harness reported
    "description is required" and the skills were invisible to a fresh agent.
    The repo's own skills (.agents/skills/**) all carry it; so must these.
    """
    front = _frontmatter(_skill_text(skill))
    assert front.get("name") == skill, f"{skill}: frontmatter name must be {skill!r}"
    description = front.get("description", "")
    assert description, f"{skill}: frontmatter description is required"
    assert description.rstrip().endswith("."), f"{skill}: the description reads as a sentence"
    assert len(description) < 400, f"{skill}: the description is a chooser, not the skill"


@pytest.mark.parametrize("skill", ["autoclip-setup", "autoclip-clip", "autoclip-help"])
def test_every_skill_frontmatter_parses_as_strict_yaml(skill: str) -> None:
    """The frontmatter must load as REAL YAML, not as the flat-pair approximation.

    ``_frontmatter`` above splits on the first ':', so it cannot see a value
    that itself contains ': ' — yet that is exactly what the agent harness's
    YAML parser rejects ("nested mappings are not allowed in compact mappings").
    autoclip-help shipped `... (ISO 24495-1:2023): what it is, ...` and the
    skill vanished from the harness until the ':' was reworded to '.'.
    """
    yaml = pytest.importorskip("yaml")
    text = _skill_text(skill)
    assert text.startswith("---\n"), f"{skill}: frontmatter must open with ---"
    block = text.split("---\n", 2)[1]
    payload = yaml.safe_load(block)  # a parse error here is the regression
    assert isinstance(payload, dict), f"{skill}: frontmatter must be a mapping"
    assert payload.get("name") == skill, f"{skill}: YAML name must be {skill!r}"
    assert payload.get("description"), f"{skill}: YAML description is required"
