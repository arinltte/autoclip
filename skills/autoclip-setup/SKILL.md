---
name: autoclip-setup
description: Ready the AutoClip folder once — venv, static ffmpeg, whisper weights, PO-token server, secrets — then interview the user into a plain settings.md. Run it after cloning or whenever `aclip doctor` reports a FAIL; re-running on a ready folder is a no-op that reports green.
---

# autoclip-setup

Get this folder ready once — the Python environment, the static ffmpeg, the
whisper weights, the PO-token server, and the secrets — and write the plain
`settings.md` the rest of the skills read. Run this once after cloning; after
that, `autoclip-clip` needs no setup. Re-running on a ready folder is a no-op
that reports green.

## Read this first — where this skill runs

This skill runs on a FOLDER, and every path it uses is relative to the
**repository root** — the folder that holds `AGENTS.md`. Run every command
below from there. If you are not sure you are at the root:

```
ls AGENTS.md || cd "$(git rev-parse --show-toplevel)"
```

The skill file and the toolchain can live in two places at once. Both are
correct at the same time:

- The **tracked tree** at the repository root (`skills/`) is the copy that
  RUNS. It holds the `aclip` package, the test suite, and every path this
  skill mentions.
- A **gitignored copy** of the same skills may sit in `.agents/skills/`. That
  copy is only how pi DISCOVERS the skill documents (pi's project-local skill
  location). It is expected, and it is not a problem.

Two rules follow:

- If pi loaded this skill from `.agents/skills/…`, expect the tracked
  `skills/` tree to be MISSING — a discovery copy is often made by MOVING the
  folder, which deletes the tracked tree. Repair it in Step 0 before you run
  anything else.
- If pi's preamble says "references are relative to the skill folder", ignore
  it here. This skill's paths are repository-root relative, not
  skill-file relative.

## The gate: `aclip doctor`

Every step below ends in the same command. Run it from the **repository root**:
the path in it is relative, so the folder you run it from matters.

```
PYTHONPATH=skills/autoclip-clip/scripts .venv/bin/python -m aclip doctor
```

(From inside `skills/autoclip-clip/scripts` the same check is
`../../../.venv/bin/python -m aclip doctor` — three levels up — or activate
`.venv` first and run plain `python -m aclip doctor`.)

It prints nothing and exits 0 when the folder is ready. When something is
missing it prints one `FAIL <name> — <fix hint>` line per problem and exits 1.
Fix what it names (the hints below tell you how), run it again. Your job is
done when it is silent. (The suite check inside `doctor` runs the bundled pytest
suite — it must stay green too.)

## Step 0 — Check the layout first (a quick check that prevents a long repair)

The `aclip` package must live at `skills/autoclip-clip/scripts/aclip/`. When it
is somewhere else — most often because the skills were MOVED into
`.agents/skills/` for pi's local skill discovery — the environment contract
resolves a root that does not hold the toolchain, and every check then names
paths that do not exist. Run this one survey command from the repository root
and read its four lines:

```
echo "== repo root:"; pwd; ls AGENTS.md || echo "NOT AT ROOT"
echo "== tracked toolchain:"; ls skills/autoclip-clip/scripts/aclip/cli.py 2>/dev/null || echo MISSING
echo "== local discovery copy:"; ls .agents/skills 2>/dev/null || echo none
echo "== git signature:"; git status --short -- skills | head -5
```

Many ` D skills/…` lines in the git signature mean the tracked tree was moved
away (a `cp -R` copy would show no deletions). The tracked tree is the copy
that runs and the copy the suite and the docs expect; a gitignored
`.agents/skills/` copy may stay on disk and is only how pi finds the skills.

Repair a missing tracked tree — including the stale-lock case:

```
if ! ls skills/autoclip-clip/scripts/aclip/cli.py >/dev/null 2>&1; then
  git restore --source=HEAD --worktree skills/ || {
    if ps aux | grep -q '[g]it '; then
      echo "a git process is running — wait for it, do not remove anything"
    else
      echo "stale .git/index.lock — removing it and retrying once"
      rm -f .git/index.lock && git restore --source=HEAD --worktree skills/
    fi
  }
fi
ls skills/autoclip-clip/scripts/aclip/cli.py && echo LAYOUT-OK
```

Leave the moved copy alone. The restored tracked tree is what every later step
runs.

## Step 1 — the settings interview (writes `settings.md`)

**How to ask.** You talk to the user directly: put the questions in your
normal reply text and wait for the answer. Write in the user's language. Give
the whole list below in one message, in order, each question with its default
shown.

Offer the shortcut first — the tracked `settings.md` in the repository root
already holds every default below, so "accept all defaults" is a complete
answer. Then walk only the questions the user wants to change.

**If the user accepts all defaults:** the file already matches — prove it with
one command, report that no edit was needed, and move on:

```
grep -E '^(model|language|similarity_threshold|prefer|parallelism|chunk_minutes|intro_sec|clip_min_sec|clip_max_sec|target_count|min_score|speed|title_card|captions):' settings.md
```

**If the user wants changes:** edit only the keys they name, in the tracked
`settings.md`. Keep the plain-language glosses — `autoclip-help` reads this
file. Ask each question in plain words, then write the answer to the key it
names (the key in backticks):

1. **Which NVIDIA model should analyse the video?** → `model:` (default: blank
   = the Coding Agent's own model). Only NVIDIA NIM models are ever used by
   this project — never configure another provider.
2. **Spoken language of the videos?** → `language:` (default: `en`). Never use
   auto-detection: whisper misdetected an English podcast as Hindi once.
3. **How similar must the two transcript tracks be to trust the API wording?**
   → `similarity_threshold:` (default: `0.85` — calibrated on a real pair; do
   not change it casually).
4. **If the two tracks disagree badly, whose wording should the captions keep?**
   → `prefer:` (default: `whisper` — also `youtube` or `both`). The clip run
   records your answer here the first time it asks; `youtube`/`both` render the
   captions line-level, because the site's timings have no word detail.
5. **How many analysis chunks at once?** → `parallelism:` (default: `1`). The
   NVIDIA key allows 40 requests/minute and a lockout costs a day — keep it 1
   unless the user explicitly asks for more.
6. **How long is an analysis chunk?** → `chunk_minutes:` (default: `6`).
7. **How long is the cold open to skip?** → `intro_sec:` (default: `90` —
   teaser montages are not clip material).
8. **Shortest clip? / Longest clip?** → `clip_min_sec:` / `clip_max_sec:`
   (default: `30` / `90`).
9. **How many clips per video?** → `target_count:` (default: `10`).
10. **Minimum score floor?** → `min_score:` (default: none — no floor).
11. **Playback speed?** → `speed:` (default: `1.3`). Explain the CEILING: a
    clip that already fits the platform's 60 s plays at normal speed, and a
    longer one speeds up just enough to fit — never more than this number.
12. **Title card** — the `title_card` keys: `enabled`, `duration_sec`,
    `fontsize`, `box`, `box_opacity`, `font`, `fontcolor`, `kicker`,
    `kicker_color`, `max_lines`, `padding_pct`, `min_fontscale`, `max_chars`?
    (defaults: on, 6 s, 46 pt, box at 0.45, white text, the `HIGHLIGHT` kicker
    in `#ffd24a`, wrapped to 2 lines with 6% of the frame kept clear, the font
    shrinking to 70% before a third line, and no fixed character cap). A long
    title wraps and never runs off the frame's edge.
13. **Captions** — the `captions` keys: `enabled`, `style` (`karaoke` or
    `line`), `fontsize`, `spoken_color`, `unspoken_color`, `outline`,
    `margin_v`? (defaults: karaoke, 54 pt, yellow on white, outline 2, margin
    420 — tuned for a 1080×1920 vertical frame).

`model:` and `parallelism:` are the only agent-only keys; the rest are safe to
hand-edit (see `autoclip-help`). Write the answers into `settings.md` before
continuing.

## Step 2 — the environment (all idempotent; skip what exists)

**First, one survey of what already exists** (then skip what is already there):

```
ls .venv/bin/python 2>/dev/null && echo VENV-OK || echo NO-VENV
ls tools/bin/ffmpeg 2>/dev/null && echo FFMPEG-PRESENT || echo NO-FFMPEG
du -sh models/hf-cache 2>/dev/null || echo NO-WEIGHTS
ls tools/vendor/bgutil-ytdlp-pot-provider/server 2>/dev/null && echo POT-OK || echo NO-POT
ls .secrets/nvidia_api_key 2>/dev/null && echo SECRET-KEY || echo NO-SECRET-KEY
```

The steps are independent of each other and of `settings.md`, so the long
installs can run in the background while the Step 1 interview happens. Give
every job its log inside ONE fixed folder, and create that folder in the same
command that launches the job:

```
mkdir -p /tmp/aclip-setup && nohup bash -c '<the job>' > /tmp/aclip-setup/<name>.log 2>&1 &
```

Never name the folder with `$$`, a timestamp, or any other per-call value: in
pi every tool call is a new shell, so a folder made in one call does not exist
in the next one, and the job's log silently disappears. After each launch,
prove the job started — its log file must exist and be non-empty:

```
ls -l /tmp/aclip-setup/<name>.log && tail -2 /tmp/aclip-setup/<name>.log
```

The five steps below are independent, except the whisper weights, which need
the venv — so the venv job chains the weights job after it. Three jobs cover
the work: venv + weights (chained), ffmpeg, and the PO-token server. The
secrets seeding is quick and runs in the foreground.

1. **The venv** — `.venv/bin/python` must exist with `youtube-transcript-api`
   (1.2.4), `mlx-whisper`, `yt-dlp` (with the bgutil plugin) and `pytest`.
   Create it on Python 3.13 — the pins target 3.13 and a newer system python
   breaks the whisper stack: `uv venv .venv --python 3.13` (uv fetches a
   managed 3.13 if none is installed) or `python3.13 -m venv .venv`, then
   `uv pip install --python .venv/bin/python \
     -r skills/autoclip-clip/scripts/requirements.txt`.
   A uv venv has no `pip`: verify the packages with
   `uv pip list --python .venv/bin/python`, never with `python -m pip` (it will
   look broken when the venv is fine).
   (The installer `tools/setup.sh` does the same first steps.)
2. **ffmpeg** — `tools/bin/ffmpeg` must be executable AND have the `drawtext`
   filter (caption rendering needs libass + freetype; Homebrew's lean build
   lacks them). Use any system ffmpeg whose `-filters` lists `drawtext`, or a
   static arm64 build. The verified download (2026-10-31):

   ```
   mkdir -p tools/bin
   TMP="$(mktemp -d)"
   curl -fsSL -o "$TMP/ffmpeg.zip" https://www.osxexperts.net/ffmpeg9arm.zip \
     && ditto -x -k "$TMP/ffmpeg.zip" "$TMP" \
     && cp "$(find "$TMP" -name ffmpeg -type f | head -1)" tools/bin/ffmpeg \
     && chmod +x tools/bin/ffmpeg
   rm -rf "$TMP"
   ```

   Prove the filter, don't assume the build: `tools/bin/ffmpeg -hide_banner
   -filters | grep drawtext`. (`tools/setup.sh` still carries two dead
   osxexperts URLs — `FFmpeg6arm64.zip` / `FFmpeg9arm64.zip`, both 404 — so if
   its ffmpeg step ends with a warning, install by hand with the command above;
   do not edit the script mid-run. If the zip name ever changes again, the
   site index at https://www.osxexperts.net lists the current
   `ffmpeg*arm*.zip` names.)
3. **The whisper weights** — they resolve from the project cache
   (`models/hf-cache`) at transcribe time: warm means zero network. Warm them
   now, from the repo root:

   ```
   PYTHONPATH=skills/autoclip-clip/scripts .venv/bin/python -m aclip.weights
   ```

   It prints the resolved path on success; a global-cache copy-in or a
   first-time download (0.8–1.6 GB) happens automatically.

   Then PROVE it — `doctor`'s weights check passes on any file in the
   snapshot, so a 300-byte fragment left by an interrupted earlier download
   still counts as warm. The proof is a real offline resolution:

   ```
   HF_HUB_OFFLINE=1 .venv/bin/python - <<'PY'
   from pathlib import Path
   from huggingface_hub import snapshot_download
   p = Path(snapshot_download("mlx-community/whisper-large-v3-turbo",
                              cache_dir="models/hf-cache",
                              local_files_only=True))
   sizes = [f.stat().st_size for f in p.rglob("*") if f.is_file()]
   print("offline-ok:", len(sizes), "files,", round(sum(sizes) / 1e9, 2), "GB")
   PY
   ```

   Expect hundreds of MB to ~1.6 GB. If `aclip.weights` fails with "did not
   resolve from the project cache after copy", your global HF cache holds a
   broken stub and its "copy" plan copied the stub — force a fresh download
   instead: clear the project copy and point `HF_HOME` at an empty throwaway
   directory for that one run (the download still lands in
   `models/hf-cache`):

   ```
   rm -rf models/hf-cache && mkdir -p /tmp/aclip-empty-global
   HF_HOME=/tmp/aclip-empty-global \
     PYTHONPATH=skills/autoclip-clip/scripts .venv/bin/python -m aclip.weights
   ```
4. **The PO-token server** — `tools/vendor/bgutil-ytdlp-pot-provider/server`
   must exist (the vendored provider for the 403-prone high formats):
   `git clone --depth 1 --branch 2.0.0 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git tools/vendor/bgutil-ytdlp-pot-provider`
   then `(cd tools/vendor/bgutil-ytdlp-pot-provider/server && deno install)`
   or `npm ci` if you have npm.
5. **The secrets** — referenced by PATH, never printed, never committed. Check
   each exists and report only its presence:
   `.secrets/nvidia_api_key`, `.secrets/youtube_client_secret.json`,
   `.secrets/youtube_token.json`. The YouTube token is created by
   `.venv/bin/python tools/youtube_upload.py --check` (uploads are a manual tool
   — the pipeline never uploads).

   When `.secrets/nvidia_api_key` is missing, do not re-ask the user — pi's own
   auth already carries the key. Seed it by script so the value is never
   printed (the same fallback `tools/env.sh` performs):

   ```
   python3 - <<'PY'
   import json, os, pathlib
   auth = pathlib.Path(os.path.expanduser("~/.pi/agent/auth.json"))
   secrets = pathlib.Path(".secrets"); secrets.mkdir(parents=True, exist_ok=True)
   key = json.loads(auth.read_text()).get("nvidia", {}).get("key") if auth.is_file() else None
   if key:
       (secrets / "nvidia_api_key").write_text(key)
       print("nvidia_api_key written (chmod 600 next; never printed)")
   else:
       print("no NVIDIA key in pi's auth.json — the user must provide one")
   PY
   chmod 600 .secrets/nvidia_api_key 2>/dev/null || true
   ```

   The two YouTube secrets are optional for the pipeline (it never uploads):
   report them as absent and move on.

## Step 3 — verify

Run the gate in the box at the top. Silent + exit 0 means ready. If `doctor`
reports a FAIL, the hint names the step above that repairs it — repair it, then
re-run both the step's proof recipe and the gate, not just the gate. Done —
tell the user to paste a YouTube link into `autoclip-clip`.

## Rules

- NVIDIA NIM is the only LLM provider for this project; 40 requests/minute —
  one model call at a time (that is why `parallelism:` defaults to 1).
- Keep your own number of model calls small: batch checks into single
  commands, do not re-plan between commands, and give the whole interview in
  one message. The same 40-requests-per-minute key serves your turns, and a
  429 wastes one.
- Never print or commit a secret; reference `.secrets/` by path only.
- The only file this skill writes in the repo is `settings.md`.
- Existence is not health: every artifact above needs the proof recipe in its
  step (drawtext in `-filters`, a real offline weights resolution) — an
  artifact that merely exists can still be a stub.
- The frozen `aclip` subcommand list is complete; setup drives the installs with
  the venv python and the commands above, not with new CLI surface.
