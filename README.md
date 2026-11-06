<img src="logo.png" alt="AutoClip" width="64">

# AutoClip — paste a link, get the clips

**Version 0.1.0 — pre-release.** AutoClip is still being tested and improved;
APIs, defaults, and render details can change between releases.

**AutoClip is a folder of three coding-agent skills.** Give your coding agent
one YouTube link and it hands you ready-to-post 9:16 Shorts — karaoke captions,
an animated title card, the right playback speed — end to end, without
supervising it. Everything the pipeline needs (tools, model weights, binaries,
state) lives inside this one cloned folder. Set it up once; after that, paste a
link.

## See what AutoClip makes

From one long talk, AutoClip pulls out the standalone moments and reframes each
into a vertical Short. Below is the *same second* of a ~70-minute BFM podcast
episode — the wide original on the left, and the clip AutoClip rendered from
that section on the right (title card + karaoke captions, reframed to 9:16).

| Original section (16:9) | AutoClip clip (9:16) |
| :---: | :---: |
| <img src="images/original-section.jpg" alt="Original podcast section — wide studio shot" width="569"><br><sub>Original episode · this section at <a href="https://youtu.be/09Dl5rO6TlY?t=1102s">▶ 18:22 on YouTube</a></sub> | <video src="https://github.com/user-attachments/assets/7ddb5137-0aa9-45c7-9b5c-39af3a9aa9c7" controls poster="images/clipped-vertical.jpg" width="200"></video><br><sub>58 s render · 9:16 · title card + karaoke captions — press play</sub> |

The clip playing on the right is
`projects/09Dl5rO6TlY/output/01__If_you_don_t_have_trust__you_can_t_grow__-_the_CarSum_lesson_ServAuto_banks_on.mp4`
(rank 1, score 94); press play to watch it.

## Run setup once, then paste a link

1. Clone this folder.
2. Ask your coding agent to run the **autoclip-setup** skill and answer its
   questions — it readies the folder (venv, static ffmpeg, whisper weights, the
   PO-token server, a secrets checklist) and writes your `settings.md`.
3. Paste a YouTube link and say "make clips". The **autoclip-clip** skill runs
   the whole pipeline and stops only for genuine defects.
4. Your clips land in `projects/<Video Id>/output/`, with a short report
   beside them.

Questions about any setting? Ask for the **autoclip-help** skill — it explains
everything in plain language and reads your live `settings.md`.

## Tested with

AutoClip is exercised end to end with the **PI Coding Agent** on two models,
both served by **NVIDIA BUILD NIM**: **Step 5 Preview** and **GLM 5.3 Flash**.

Other coding agents and other models are **untested** — AutoClip may still work
on them, but nothing is guaranteed. Since this is a v0.1.0 pre-release, if
something breaks please [open an issue](https://github.com/arinltte/autoclip/issues)
with the steps you ran so the problem can be reproduced and fixed.

## Let your agent see the skills (local install)

Agents that follow the Agent Skills convention (pi, and others) look for
project skills in `.agents/skills/`. To install the three skills there,
**copy** the folders — never move them:

```
mkdir -p .agents/skills && cp -R skills/autoclip-setup skills/autoclip-help skills/autoclip-clip .agents/skills/
```

A move would delete the tracked `skills/` tree — the pipeline, the tests, and
`aclip doctor` all run from it. The `.agents/` folder is gitignored on
purpose, so the copy stays on your machine. When you update the skills, edit
the tracked `skills/` copies and copy again. If the tracked tree is ever
missing, the **autoclip-setup** skill repairs it in its Step 0.

## The pipeline

```
 YouTube link
     │
     ▼
 scaffold ──► preflight ping ──► download ──► transcript-fetch ──► transcribe
 (projects/)   (a real model      (3-rung       (YouTube track)     (whisper track,
               call, 512 tok)     ladder)                           word timings)
                                                     │
                 ┌───────────────────────────────────┘
                 ▼
            compare ──► analyse ──► curate ──► render ──► output/
         (both tracks,   (one subagent   (deterministic  (concurrent ffmpeg,
          0.85 ask)      per chunk,      top-N)          karaoke + title card)
                          sequential)
                 │
                 ▼
        projects/<Video Id>/output/REPORT.md
```

Every deterministic step is a subcommand of the bundled `aclip` CLI
(`doctor`, `scaffold`, `download`, `transcript-fetch`, `compare`, `transcribe`,
`render`); analysis is the only part that stays agent work — one subagent per
chunk, strictly sequential, with a per-chunk cache so a re-run costs nothing.

## The folder map

```
AutoClip/
├── skills/                     the three skills
│   ├── autoclip-setup/         environment + the settings interview
│   ├── autoclip-clip/          the pipeline (SKILL.md + the bundled aclip CLI)
│   └── autoclip-help/          plain-language explanations
├── images/                      README screenshots (comparison stills)
├── settings.md                 your configuration (setup writes it; safe to hand-edit)
├── AGENTS.md                   the contract for the coding agent that drives this
├── projects/<Video Id>/        your per-video state (download/transcript/analyze/output)
├── tools/                      the sidecar tools (transcribe, upload, setup.sh, vendored ffmpeg + PO-token server)
└── models/                     the whisper weights (downloaded on first transcribe; not committed)
```

`.venv/`, `models/`, `tools/bin/`, `tools/vendor/`, `.secrets/`, `projects/`,
`GLOSSARY.md` and `docs/adr/` are local-only (gitignored) — a fresh clone
bootstraps the runtimes with `tools/setup.sh` or the setup skill.

## Out of scope

Uploading is not part of the pipeline. `tools/youtube_upload.py` stays as a
manual tool you can run yourself.

## Attribution

Analysis runs on your coding agent's model; this project's only configured LLM
provider is **NVIDIA NIM** (`integrate.api.nvidia.com`), whose key allows 40
requests per minute — hence the strictly sequential analysis. Captions are
rendered by a static ffmpeg build with libass; transcription is MLX Whisper.
