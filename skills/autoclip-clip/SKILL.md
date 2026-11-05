---
name: autoclip-clip
description: Run the whole AutoClip pipeline unsupervised from one YouTube link — download, both transcript tracks, compare, analyse, curate, render — into ready-to-post 9:16 clips with karaoke captions and a title card, stopping only for the four sanctioned interrupts.
---

# autoclip-clip

Paste one YouTube link; get finished 9:16 clips in `projects/<Video Id>/output/`
without supervising anything. This skill is the whole pipeline: download, both
transcript tracks, compare, analyse, curate, render. You — the Coding Agent —
are the pipeline: every mechanical step is an `aclip` subprocess, and the
analysis is one sequential subagent per chunk. Stop ONLY for the four
interrupts in the list below; everything else ends with a written report.

## Before anything: the gate

```
cd skills/autoclip-clip/scripts && python -m aclip doctor
```

Silent + exit 0 → ready. `FAIL <name> — <hint>` → stop and run `autoclip-setup`
(its steps repair what doctor names). Do not continue on a red doctor.

**Running every command below.** The interpreter and the working folder cost
every fresh session a discovery loop, so they are fixed here. Run everything —
the `aclip` subcommands *and* the `aclip.analysis` module entries — from
`skills/autoclip-clip/scripts`, because that is where the `aclip` package lives:
a `python -m aclip…` run from anywhere else raises `ModuleNotFoundError`. `python`
is often not on PATH; when it is missing use the project interpreter
`.venv/bin/python`. Every `python` written in this skill means "your
interpreter". Pass Project paths **absolute** (`<repo>/projects/<Video Id>`); a
relative path fails.

All paths in this skill are relative to the **repository root** — the folder
that holds `AGENTS.md` — and to the tracked `skills/` tree there. If pi loaded
this skill from `.agents/skills/`, that copy is only how pi found it: run the
pipeline from the tracked tree, and if that tree is missing, run
`autoclip-setup` first (its Step 0 repairs it).

## The pipeline (in order)

The Video Id comes from the link (`watch?v=`, `youtu.be/`, `/shorts/`…). Name it
`<Video Id>` below; the Project is `projects/<Video Id>/`.

1. **Scaffold** — `python -m aclip scaffold <Video Id>`.
2. **Preflight ping — a REAL call, before anything expensive.** With the
   `.secrets/nvidia_api_key` (by path, never printed), make one real call to
   `https://integrate.api.nvidia.com/v1/chat/completions` with
   `"max_tokens": 512` and a one-word prompt, against the model that will
   analyse (settings.md `model:` when set, else your own). A non-2xx response
   stops the run only after you classify it — quote the exact HTTP status
   either way, before the download, not after:
   - **Dead model or dead auth — 401, 403, 404, 410 — STOPS the run
     immediately** (a 410 means the model is dead). No retry: the answer will
     not change, and this is the interrupt.
   - **Transient gateway or traffic — 429, 502, 503, 504, or a connection
     error — is NOT a stop.** The NVIDIA gateway times out on this reasoning
     model even for a one-word prompt (a real run got two consecutive 504s,
     then a 200 three minutes later on the same key). Retry the SAME ping
     with backoff before believing it — three attempts, `sleep 60` between,
     one command so it costs one turn:

     ```
     for i in 1 2 3; do
       HTTP=$(curl -s -o /tmp/preflight_out.json -w "%{http_code}" \
         https://integrate.api.nvidia.com/v1/chat/completions \
         -H "Authorization: Bearer $(cat $ROOT/.secrets/nvidia_api_key)" \
         -H "Content-Type: application/json" \
         -d "{\"model\": \"${MODEL:-$PI_MODEL}\", \"max_tokens\": 512, \"messages\": [{\"role\": \"user\", \"content\": \"ping\"}]}")
       [[ "$HTTP" == 2* ]] && break
       [ "$i" = 3 ] || sleep 60
     done
     echo "HTTP status: $HTTP"
     ```

     Three attempts are at most three requests on the 40-requests/minute
     key. A status that is still transient after the third attempt — THAT is
     interrupt #1: quote the exact status.
   - A 2xx response is green: proceed to the download.
   The model that answers is `settings.md`'s `model:` when set, otherwise the
   session's own model (`echo $PI_MODEL`) — the same model that analyses in
   step 6, so the two never drift apart.

   The three variables every command below needs cost a discovery loop when a
   session works them out one at a time, so resolve them all in ONE command,
   before the ping, and reuse them. `PY` is the project interpreter (`python`
   is often not on PATH), `ROOT` the repository root (the folder holding
   `AGENTS.md`), `PID` the Video Id:

   ```
   ROOT=/Users/chenjinshen/Downloads/testingclip
   PY=$ROOT/.venv/bin/python
   PID=Ane7U3mRS8w
   PROJ=$ROOT/projects/$PID
   cd $ROOT/skills/autoclip-clip/scripts
   echo "interpreter=$(test -x $PY && echo ok || echo MISSING) root=$(test -f $ROOT/AGENTS.md && echo ok || echo MISSING)"
   ```

   Every command below assumes that cwd and writes `$ROOT/skills/...` as
   `cd skills/autoclip-clip/scripts` only for brevity. Pass Project paths
   absolute (`$PROJ`); a relative path fails.
3. **Download** — `python -m aclip download <link>` (the fixed three-rung ladder;
   the vendored PO-token provider handles the 403-prone formats). Resume is
   automatic. All rungs exhausted is a hard failure (interrupt).
4. **Both transcript tracks** — `python -m aclip transcript-fetch <Video Id>`
   (the YouTube Track; no captions → the run goes whisper-only, silently) and
   `python -m aclip transcribe <Video Id>` (the Whisper Track: word-level
   timings, the weights warm from the project cache).
   A transcription with no words at all (zero-speech media) is a hard failure
   (interrupt).
5. **Compare** — `python -m aclip compare <Video Id>` reads
   `projects/<Video Id>/transcript/compare_report.json`:
   - at/above the settings threshold (`0.85`): adopt the whisper wording,
     continue silently. The report may still list a few `diverged_minutes`
     (per-minute similarity dips) — that is ordinary drift, not a defect, and it
     is **not** what interrupt #4 measures. Only `flagged_spans`
     (no-speech / low-logprob audio) counts, and only its `flagged_fraction`
     against `duration_sec` can stop the run. A run can carry several diverged
     minutes with a flagged fraction of 0.0 and still be a clean adopt.
   - below: ASK ONCE — "which wording should the captions use: A whisper /
     B youtube / C both?" (the report carries ~5 sample divergences). Record
     the answer in `settings.md` as `prefer:` and never ask again. B/C then
     render the YouTube wording on line-level timings (word timings are never
     invented), so the karaoke captions become whole-line captions.
6. **Analyse** — the only model work, and it stays yours to spawn:
   - `python -m aclip.analysis plan projects/<Video Id>` (a module entry, NOT
     an `aclip` subcommand) prints the dry-run plan: chunk boundaries, cache
     hits/misses, `to_query`. Zero model calls. Re-run it when this step ends:
     a finished run reports `cache_hits` equal to the chunk count and
     `to_query: []`.
   - For each chunk in `to_query`, **strictly one subagent at a time**
     (`parallelism: 1` — the NVIDIA 40-requests/minute rule). The spawn is a
     tool call and it is the most failure-prone line in this skill. The shape
     is exact — copy it, substituting the placeholders:

     ```
     subagent({
       agent: "delegate",
       task: <the filled analysis-prompt.md — preamble + prompt, verbatim>,
       async: false,
       cwd: "<repo>/projects/<Video Id>",
       output: "<repo>/projects/<Video Id>/analyze/.chunk_NNN.result.md",
       options: { thinking: "high" }
     })
     ```

     - **`task` is required — it is the instructions.** `output` only names
       where the child's summary lands; it is not an instruction. A call that
       sets `output` and omits `task` still launches, and the child receives no
       mission at all: it explores the repository, burns the
       40-requests/minute quota, trips a 429, and the run fails. Every spawn
       carries `task`.
     - **Verify the mission actually shipped — this model drops the long `task`
       argument.** A reviewed run (the 09Dl5rO6TlY session) wrote the whole
       ~4,000-character prompt into its prose and still emitted the spawn
       WITHOUT `task` — nine of fourteen chunks. A dropped `task` is a parent
       call-fault, not a model fault, and nothing rejects it: the child recovers
       its mission by reading the repo (a runaway that burned 20–90 minutes and
       real 429s per child) or times out. pi echoes the call's arguments back in
       the spawn result — **so after every spawn returns, confirm the recorded
       call carried a non-empty `task`; if it did not, treat the chunk as never
       launched (see `references/analysis-spawn.md` Branch 1) and re-issue it
       with the prompt.** Never keep a task-less child's array just because it
       "looks right" — that "recovery" is the runaway, so re-spawn instead. Keep
       the prompt inline as `task`; do not point the child at a file to read
       instead (reading it costs a child turn the 3-turn budget cannot spare).
     - **`async: false`** blocks until the child finishes, and that is what
       keeps the chunks strictly sequential. Never launch children
       asynchronously and wait, and never place two spawns in one turn: one
       spawn, then its validate + record, then the next chunk.
     - **`delegate`** is the sanctioned child: it inherits the analysing
       model, starts with no default reads, and holds no `subagent` tool, so it
       cannot spawn children of its own and cascade the quota. Do not guess
       other agent names. Pass no `parallelism` option — it is not a valid key
       and the call is rejected outright.
     - The reasoning model is `settings.md`'s `model:` when set, otherwise the
       session's own model (`echo $PI_MODEL`). The prompt's
       `Model to reason with` line already carries that, so `model:` stays blank
       by default — never override it here.
   - Pointers, not payloads: the child reads the transcript artifact itself and
     answers by writing `analyze/chunk_NNN.candidates.json` — the raw answer
     file. Validate that file, then record it:

     ```
     python -m aclip.analysis validate <repo>/projects/<Video Id>/analyze/chunk_NNN.candidates.json
     python -m aclip.analysis record <repo>/projects/<Video Id> NNN <repo>/projects/<Video Id>/analyze/chunk_NNN.candidates.json
     ```

     `validate` checks the `.candidates.json` the child wrote; `record`
     installs it as `analyze/chunk_NNN.json`, the cache — a
     `{chunk, model, items}` wrapper. Never point `validate` at the cache
     file: a wrapper is always rejected. Read coverage by re-running the plan,
     not by validating.
   - **The write is the part children get wrong.** This model reliably prefers
     to print its JSON in its reply over writing the file: in the reviewed run
     (#38) four of seven chunks returned a complete, conforming array in the
     report and wrote nothing. That is NOT a failure of the analysis — the
     answer exists and only the write is missing. So:
     - the child's report lands at `analyze/.chunk_NNN.result.md`;
     - when the answer file is **missing** but the report holds a complete
       JSON array, install that array at the answer path yourself — copy the
       brackets out, add nothing, change nothing — then run the SAME
       `validate` + `record` gate as every written file. Record it and note
       the reconstruction in the report. **Do not re-spawn** for this: a
       re-spawn costs a model call and produces the same reply.
     - re-spawn only when there is no array anywhere to recover (no file and
       no array) — see `references/analysis-spawn.md`.
   - **One spawn, then one turn that records it AND spawns the next.** The
     sequence for chunk N is: spawn (this turn ends) → next turn does
     validate + record for chunk N *and* the spawn for chunk N+1 in the same
     reply. Never two spawns in one turn; never wait a whole turn to record.
     Seven spawns then seven records burns twice the turns on a
     40-requests/minute key.
     The spawn inside that combined turn is the same guarded call — exactly one,
     and confirm its `task` echoed back before you trust the chunk launched. A
     task-bearing child is bounded by its preamble (at most 3 turns, one file)
     and finishes in minutes; only a task-less child can run for tens of minutes,
     so the guard above is also what keeps every child short.
   - A schema violation is the subagent's to fix. When a spawn misbehaves — a
     429, a malformed call, a child whose report came back empty, a file that
     fails validation — follow `references/analysis-spawn.md`; none of it is
     one of the four interrupts. A chunk that recorded is cached and is never
     spawned again.
7. **Curate** — `python -m aclip.curate projects/<Video Id>` writes TWO
   files and is the deterministic selection (bounds, intro skip, overlap
   merge, score order — no taste, no calls):
   - `analyze/selection.json` — the clips the render renders: the top
     `target_count` of the ranking (10 by default), each carrying its own
     `rank`;
   - `analyze/candidates.json` — **the full analysis**: every cached
     Candidate, best-first, with the disposition that explains it
     (`selected`, `available` = ranked well but past the cut, `merged` into a
     higher-scored keeper, or `dropped_intro` / `dropped_out_of_bounds` /
     `dropped_below_floor`).
   The selection is the top 10; the analysis is all of it. They are different
   numbers and both belong in the report — "10 clips from 39 candidates" is
   the line, with the rest of the inventory behind it.
   `python -m aclip.curate <project> --skip N` selects ranks N+1..N+10
   instead (see step 10). `--count N` narrows the batch.
8. **Render** — `python -m aclip render <Video Id>` renders the selection
   concurrently (the one parallel stage; plain ffmpeg, no API, no limit).
   Two render behaviours are per clip, never global:
   - **speed** — `settings.md`'s `speed` is the CEILING, not a default. A clip
     that already fits the platform's 60 s renders at **1.0**; a longer one
     takes the smallest ladder step (1.1, 1.2, 1.25, …) that fits it, never
     more than the ceiling. Read the per-clip values from the render output
     (`rendered: … (speed 1.25, 57.9s)`), not from settings.
   - **title** — the title card wraps a long title to `max_lines` lines
     (2 by default), keeps `padding_pct` of the frame clear of text, and
     shrinks the font to `min_fontscale` before a third line is used. A title
     is never truncated and never runs past the frame's edge.
9. **Report** — write `projects/<Video Id>/output/REPORT.md`: the coverage
   (chunks total vs queried vs cached), the similarity score and the compare
   decision, the flagged spans, every rendered clip's path **with its speed
   and output duration**, and the full analysis (`analyze/candidates.json`'s
   candidate count with the disposition counts). The pipeline never uploads —
   `tools/youtube_upload.py` remains a manual tool. Report the anomalies the run
   actually had, counted from the record and not from memory: re-read this
   session's own spawn calls and state how many shipped without `task`, plus
   every 429 and re-spawn you saw. A run that under-reports its own respawns
   (the reviewed one wrote "three" for nine task-less spawns and "no 429" while
   two runaway children hit the limit) teaches the next reviewer nothing — the
   report is the only durable evidence of what went wrong.
10. **Continue (only when the user asks)** — the analysis cache already holds
   every Candidate, so clipping the rest costs no model call. Count what has
   rendered, then curate from that offset and render again:

   ```
   ls $PROJ/output/*.mp4 | wc -l
   python -m aclip.curate $PROJ --skip <that count>
   python -m aclip render <Video Id>
   ```

   Ranks are carried in the filenames (`11_….mp4`), so a continuation batch
   never overwrites the first ten. Re-running `curate` with `--skip 0` rewrites
   the selection from the top, deterministically.

## The interrupt list — COMPLETE, all four, nothing else

1. **Preflight failure** — a dead model or dead auth (401/403/404/410), or a
   transient status that survived the bounded retries: the ping's exact HTTP
   status, quoted before the download.
2. **Download exhausted** (all three rungs) or **zero-speech media** — the
   exact last error / the zero-word transcription.
3. **Divergence below threshold** — the one question above, asked once.
4. **Flagged spans over ~10% of the duration** — the report's
   `flagged_fraction` against its `duration_sec`; the user decides whether to
   continue.

Everything else ends SILENT with the end-of-run report. Do not ask about
cosmetics, formats, or the GUI-like details. A missing YouTube track, a
re-download, a cache replay: silent.

## Rules

- NVIDIA NIM is this project's only LLM provider; 40 requests/minute. One
  model call at a time during analysis — render parallelises, nothing else. A
  429 is a signal to back off and re-spawn the same chunk, never a reason to
  run two children at once and never a stop: see `references/analysis-spawn.md`.
- Secrets by path, never printed. Cached chunks never re-queried; transcripts
  never re-fetched. An analysis answer the child already produced is never
  re-derived — recovering it from the report is recording, not re-analysing.
- You are the only source of analysis; the CLI never calls a model.
- Vocabulary: Project, Candidate, Rendered Clip — `GLOSSARY.md`.
