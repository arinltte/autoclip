# AutoClip

A folder of coding-agent skills that turn one YouTube link into ready-to-post
vertical clips. The Coding Agent is the pipeline; the bundled `aclip` CLI does
every mechanical step and never calls a model.

## Language

**Project**:
One run's workspace: `projects/<Video Id>/`, holding the download, both
transcript tracks, the analysis cache, and the rendered output.
_Avoid_: job, task, workspace folder.

**Video Id**:
The YouTube identifier from the link (`watch?v=`, `youtu.be/`, `/shorts/`),
which names the Project's folder.

**YouTube Track**:
The transcript fetched from YouTube's own caption track, worded by the site and
timed at line level.
_Avoid_: API track, site track.

**Whisper Track**:
The transcript produced locally by Whisper, with word-level timings.
_Avoid_: local track, whisper transcript.

**Chunk**:
One analysis window of `chunk_minutes` minutes, skipping the first
`intro_sec` seconds. Exactly one subagent per Chunk, run sequentially.

**Candidate**:
A moment a Chunk's analysis found worth clipping: a score, a title, a reason,
and video-absolute start/end seconds.

**Candidate Inventory**:
`analyze/candidates.json` — every cached Candidate, best-first, each with the
disposition that explains it (selected, available, merged, or dropped).
_Avoid_: analysis report, full list.

**Disposition**:
Why a Candidate is or is not in the selection: `selected` (this batch renders
it), `available` (ranked well but past the cut), `merged` (absorbed by a
higher-scored keeper), `dropped_intro`, `dropped_out_of_bounds`, or
`dropped_below_floor`.

**Rendered Clip**:
One finished 9:16 `.mp4` in `projects/<Video Id>/output/`, captions and title
card burned in, named `NN_<title>.mp4` where NN is its rank.

**Selection**:
`analyze/selection.json` — the ranked clips the render renders; the top
`target_count` of the ranking by default.

**Title Card**:
The animated overlay at the top of a Rendered Clip: a kicker line and the
title, wrapping to at most `max_lines` lines within the frame's padding.

**Session Log**:
The `.jsonl` record of one agent run under `~/.pi/agent/sessions/`; the folder
name encodes the run's working directory. How a run is reviewed — not memory.

**Test Clone**:
A throwaway copy of this repository under `~/Downloads/`, made by copying the
tracked `skills/` tree into `.agents/skills/`. Deleted once its review is done;
edits are never made in one.

**aclip**:
The bundled Python CLI (`skills/autoclip-clip/scripts/`): doctor, scaffold,
download, transcript-fetch, transcribe, compare, analysis, curate, render.

**Interrupt**:
One of the four conditions that legitimately stops a run: preflight failure,
download exhaustion or zero-speech media, below-threshold divergence, or
flagged spans over ~10% of the duration. Everything else ends silent with a
report. A preflight failure is a dead model or dead auth (401/403/404/410),
or a transient status (429/5xx) that survived the bounded retries — one
gateway timeout is retried, never a stop.
