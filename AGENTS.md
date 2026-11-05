# AGENTS.md — contract for the Coding Agent that drives the AutoClip skill set

AutoClip is a folder of three coding-agent skills — `autoclip-setup`,
`autoclip-clip`, `autoclip-help` — driven by one bundled Python CLI, `aclip`
(shipped into `skills/autoclip-clip/scripts/`). This file is the contract for
the Coding Agent that runs them.

## LLM provider
NVIDIA NIM is this project's only LLM provider. Every model call — analysis,
health pings, model tests, everything — goes to `integrate.api.nvidia.com` with
the project's NVIDIA key. Never configure or use any other provider — not
OpenRouter, OpenAI, Anthropic, or any gateway, not even "just this once".

## If your model is NVIDIA NIM
The key is rate-limited to 40 requests/minute. Make one LLM call at a time: run
the pipeline sequentially, and only render clips in parallel. Never fire
concurrent or sub-spawned model calls — going over the limit locks the account
for a day.

The same key also serves YOUR turns while you drive these skills. Batch your
checks into single commands, do not re-plan between commands, and give the
whole settings interview in one message. A 429 on your turn is retried for
you, but it costs minutes of wall-clock, and the reviewed sessions hit it four
and five times each — a burst right after the user answers is the common
pattern.

## Secrets
Reference secrets by file path (they live under `.secrets/`); never print, echo,
or inline a key.

## Caches
A cached analysis chunk is never re-queried, and a transcript is never
re-fetched — replay the cache instead of burning quota on a result you already
have.

One machine-level broken cache, known: the global Hugging Face cache on this
Mac holds a stub for `mlx-community/whisper-large-v3-turbo` (a few fragments,
about 28 KB, under `~/.cache/huggingface/hub/`). The first weights warm in
every test clone therefore fails with "did not resolve from the project cache
after copy" — the recovery recipe in the setup skill's Step 2.3 handles it
(clear `models/hf-cache`, point `HF_HOME` at an empty throwaway directory,
re-run). The one-time fix on this machine is to delete that stub directory; do
that and later clones take the normal warm path.

## Vocabulary
Use the terms in the root `GLOSSARY.md` — Project, YouTube Track, Whisper Track,
Chunk, Candidate, Rendered Clip, and the skill names. The app era's machinery
(Zones, Model Chain, Spread Schedule, Benched) is retired and stays in git
history on `main`; add any new concept to the GLOSSARY.

Two terms this file adds: **Test Clone** (a throwaway clone in `~/Downloads/`)
and **Session Log** (the `.jsonl` record of one run). Add both to the GLOSSARY.

## Where edits go — this folder, never the test clone
Edit the skills, the CLI, and the tools in THIS folder only. It is the tracked
source of truth. A test clone is a copy: an edit made there is deleted with the
clone, and moving `skills/` inside a clone breaks the clone's worktree. Every
future session starts here: edit, commit, push — then exercise the change in a
test clone.

## Test clones (`yt`, `yt2`, `yt3`, …)
The folders `yt`, `yt2`, `yt3`, and the like (under `~/Downloads/`) are Test
Clones. They are throwaway: each one is DELETED once its review is done. They
hold no edits, no secrets, and no runtime state worth keeping —
`autoclip-setup` rebuilds everything a clone needs.

pi discovers project skills in `.agents/skills/`, so a clone needs the three
skill folders there. COPY them — never move them:

```
mkdir -p .agents/skills && cp -R skills/autoclip-setup skills/autoclip-help skills/autoclip-clip .agents/skills/
```

A move deletes the tracked `skills/` tree from the clone's worktree and breaks
`aclip doctor`; the setup skill's Step 0 repairs it, but the copy avoids the
detour. `.agents/` is gitignored, so the copy is local by design.

The next review continues with the main autoclip features — the `autoclip-clip`
pipeline run — reviewed the same way, edited the same way: in this folder.

## Reading session history — with a script, not the read tool
Every clone's run writes a Session Log: a `.jsonl` file under
`~/.pi/agent/sessions/` (the folder name encodes the clone's path). Review a run
from its log, not from memory. The `read` tool truncates a log at about 50 KB —
half of a real session — and past that limit sits the agent's private
reasoning, which is exactly the part that shows what confused it.

Use the script, which prints the whole log (thinking, tool calls, results, then
a summary):

```
python3 tools/read_session.py ~/.pi/agent/sessions/--Users-chenjinshen-Downloads-yt3--/<file>.jsonl
```

An equivalent inline script is fine too. Reading the raw file with the read
tool is not.

## Response language
Answer in the user's language. The user of this project writes English: reply
in English, run the settings interview in English, and never switch language
mid-session — an earlier run lost turns to that.

## Before you commit
- Run the bundled suite from the repo root:
  `PYTHONPATH=skills/autoclip-clip/scripts .venv/bin/python -m pytest skills/autoclip-clip/scripts/tests -q`
- `tests/test_skill_docs.py` pins the SKILL.md section headings, the settings
  interview's keys, and the README's shape. Read it before you edit a skill
  document.
- Write the commit message in the repo's short style, and say WHY: which part
  of a run was confused, and what changed to remove it.

## The app is gone
The native macOS app (`src/`, `engine/`, `tests/`, `hooks/`, `Cargo.*`) was
deleted at the #37 cutover — it lives in git history on `main`. Only the skill
set remains: `skills/`, `settings.md`, `AGENTS.md`, this `README.md`, `logo.png`,
`projects/` (runtime state), and the sidecar `tools/` (transcribe, upload,
`setup.sh`, plus the gitignored `tools/bin` ffmpeg and `tools/vendor` PO-token
server). Do not import or run any retired app file from the skills.
