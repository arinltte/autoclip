---
name: autoclip-help
description: Explain AutoClip in Plain Language (ISO 24495-1:2023). What it is, what you do, where your clips land, what every settings.md key means, and when it will ask you something. Read-only — it changes nothing by itself.
---

# autoclip-help

What AutoClip is, what you do, and what every setting means — in plain words.
This skill explains the folder to you. It never changes anything by itself,
except when you ask it to edit a setting with you.

## What this is

AutoClip turns long videos into short vertical clips that are ready to post.
You give it one link. It downloads the video, listens to it, finds the good
moments, and writes finished clips with captions and a title card. You do not
watch it work.

Three skills share this folder:

- **autoclip-setup** gets the folder ready (one time, after you clone it).
- **autoclip-clip** does the work — paste it a link.
- **autoclip-help** is this page.

## What you do

1. Once, after cloning: ask your coding agent to run `autoclip-setup` and answer
   its questions. It writes a file called `settings.md` in this folder.
2. Then, any time: paste a YouTube link into your coding agent and say
   "make clips". That is the whole job.

## Where your clips land

Every video gets its own folder: `projects/<Video Id>/`. Inside it:

- `download/` — the video it downloaded.
- `transcript/` — what was said, with timings (two copies: the site's captions
  and the tool's own listening).
- `analyze/` — which moments the analysis picked, saved so it never pays twice.
- `output/` — your finished clips and a short report. This is the folder you
  want. Open it.

## Your settings, explained

Read the table below against the **live** `settings.md` in this folder — open
that file and show its current values next to this table. The file always wins;
these words only explain it. Write a value by editing the file. Two keys are
**agent-only** (your coding agent sets them; leave them alone):
`model` and `parallelism`.

| Key | What it does | Safe to hand-edit? |
| --- | --- | --- |
| `model` | Which NVIDIA model does the thinking. Blank = your agent's own model. | **Agent-only** |
| `language` | The spoken language of the videos. `en` by default. Never use `auto` — it once heard an English podcast as Hindi. | Yes — but know what you speak |
| `prefer` | Whose wording the captions keep when the two tracks disagree badly: `whisper`, `youtube` or `both`. Set once, by the run, when it asks you. | Yes — but let the ask set it |
| `similarity_threshold` | How alike the two transcripts must be before the tool trusts the site's wording. `0.85` is measured, not guessed — leave it. | Yes — but changing it changes when you get asked |
| `parallelism` | How many analysis chunks run at once. Keep `1` — the model key allows 40 calls a minute and a day-long lockout. | **Agent-only** |
| `chunk_minutes` | How much audio each thinking step covers. `6` minutes is a good slice. | Yes |
| `intro_sec` | How many seconds at the start are teaser and skipped. `90`. | Yes |
| `clip_min_sec` / `clip_max_sec` | The shortest and longest clip it will make. `30` / `90` seconds. | Yes |
| `target_count` | How many clips to aim for per video. `10`. | Yes |
| `min_score` | A quality floor. Empty = no floor. | Yes |
| `speed` | The playback-speed ceiling. A clip that already fits 60 s plays at normal speed; a longer one speeds up just enough to fit, never more than this. `1.3`. | Yes |
| `title_card` | The intro card: `enabled`, `duration_sec`, `fontsize`, `box`, `box_opacity`, `font`, `fontcolor`, `kicker`, `kicker_color`, `max_lines`, `padding_pct`, `min_fontscale`, `max_chars`. | Yes — these are cosmetics |
| `captions` | The burned-in words: `enabled`, `style` (`karaoke` = word-by-word highlight; `line` = whole line at once), `fontsize`, `spoken_color`, `unspoken_color`, `outline`, `margin_v`. | Yes — these are cosmetics |

Colours are hex codes like `#ffff00`. Empty `font` means "use a system font".

### Two things that are decided per clip, not once

**Speed.** Only clips that need it speed up. Short ones play exactly as
recorded; a clip that would run past the platform's 60 seconds speeds up by
the smallest step that fits it. Your `speed` setting is the ceiling, not the
rate every clip gets.

**Title.** A long title wraps onto a second line and keeps clear of the
frame's edges. Nothing is cut off — if a title is still too long at the
smallest allowed font, it simply uses a third line instead of being cropped.

## When it will ask you something

It stays quiet on success. It asks you in exactly four cases:

1. The model is dead or the key is bad — it stops before downloading anything and
   shows you the exact error.
2. The download failed on all three quality tries, or the video has no speech.
3. The two transcripts disagree too much — it asks once which wording to keep
   (A whisper / B youtube / C both), then remembers and never asks again.
4. Large parts of the video are music or silence (more than about a tenth of
   it) — it shows you where and asks whether to continue.

Every other ending is silent: the report in `output/` says what happened.

## If something looks wrong

- Run `python -m aclip doctor` in `skills/autoclip-clip/scripts` — the tracked
  folder at the repository root (the folder with `AGENTS.md`), not the
  `.agents/skills/` copy if you have one. It prints nothing when all is well
  and names the broken piece otherwise.
- Re-running the same link costs nothing: downloads, transcripts, and analysis
  chunks are remembered. It only pays for new work.
- Uploading is not part of this tool. `tools/youtube_upload.py` is a manual
  tool you can run yourself.
