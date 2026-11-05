# Analysis prompt — one Chunk per subagent (template)

The prompt the **clip skill (#33)** hands each fresh subagent during analysis:
one subagent per Chunk, run **strictly sequentially** (settings `parallelism: 1`
is the default and the NVIDIA 40-requests/minute rule in AGENTS.md; the plan's
`sequential` field reports it). The skill fills every `{{placeholder}}` from the
dry-run plan (`python -m aclip.analysis plan <project_dir>`) and `settings.md`,
then hands the whole prompt **verbatim** to one subagent per MISS Chunk (the
plan's `to_query` list — a cached Chunk is never handed to a subagent).

The design is **pointers, not payloads**: the subagent reads one transcript
artifact, for one time window, and answers with one small JSON array. It never
sees the rest of the repo, the rest of the pipeline, or the other Chunks.

## How to fill the placeholders

| Placeholder | Filled from |
| --- | --- |
| `{{chunk_name}}` / `{{chunk_index}}` / `{{chunk_count}}` | plan entry `name` / `index` / `chunk_count` |
| `{{start_sec}}` / `{{end_sec}}` | plan entry `start` / `end` (inclusive, video-absolute seconds) |
| `{{start_ts}}` / `{{end_ts}}` | plan entry `start_ts` / `end_ts` (`H:MM:SS`) |
| `{{transcript_path}}` | the cue file: the single `*.cues.json` in `projects/<Video Id>/transcript/` |
| `{{output_path}}` | `projects/<Video Id>/analyze/chunk_NNN.candidates.json` (the skill's chosen answer path; NOT the cache name) |
| `{{interpreter}}` | the project interpreter, `.venv/bin/python` (SKILL.md: `python` is often not on PATH) |
| `{{model}}` | settings.md `model:` override, or the sentence below when blank |
| `{{language}}` | settings.md `language:` |

**Which artifact the child reads — decided once.** Duration comes from the
Whisper `*.words.json`, but the child reads captions, so `{{transcript_path}}` is
the single `*.cues.json` in the Project's `transcript/` folder: a JSON list of
`{"start", "end", "text"}` cues. The child never reads the word-level file and
never re-derives chunk math — the plan entry's `start`/`end` are the window
bounds it applies.

**Print the window, don't write it to a scratch file.** Turn 1's command
prints the window's cues to stdout and the child reads them from there.
An earlier revision wrote the cues to a scratch file and told the child to read
it back, which cost a fourth tool turn and, on a 40-requests/minute key, a
whole re-spawn when the child ran out of turns. The child's only written
artefact is the answer file.

**The parent's report file.** The child's *reply* still lands at
`analyze/.chunk_NNN.result.md` (the spawn call's `output:` names it) and that is
fine: the parent only reads `{{output_path}}` for data, and reads the report
purely to notice Branch 2b (an array that needs installing) in
`analysis-spawn.md`.

## The prompt (hand this verbatim)

```text
OPERATING CONSTRAINTS — a rate-limited account; every model request counts. Make
AT MOST 3 tool turns total and explore nothing: no ls/find/grep, no reading
AGENTS.md, skill files, or any file other than the one named below.

Turn 1 — run this one command (it prints ONLY your window's cues to stdout), and
read the output it prints:

{{interpreter}} - <<'PY'
import json
src = '{{transcript_path}}'
cues = [c for c in json.load(open(src)) if c['start'] >= {{start_sec}} and c['end'] <= {{end_sec}}]
for c in cues:
    print('[%.2f - %.2f] %s' % (c['start'], c['end'], c['text']))
print(len(cues), 'cues in window')
PY

Turn 2 — WRITE your Candidates to the file. This is the deliverable, and it is
the only thing the parent reads.

    {{interpreter}} - <<'PY'
    import json
    json.dump([
      # your Candidates here, in this window
    ], open('{{output_path}}', 'w'), ensure_ascii=False, indent=2)
    print('wrote', '{{output_path}}')
    PY

Turn 3 — verify the file is there, then reply with the single word: done

    {{interpreter}} -c "import json;print(len(json.load(open('{{output_path}}'))),'candidates')"

---

You are analysing ONE chunk of a podcast/video transcript to find the moments
worth clipping. One subagent, one chunk. Do not explore, ask, fetch, or
re-scope anything beyond it.

Model to reason with: {{model}}
Language of the recording: {{language}}

YOUR CHUNK — pointers, not payloads
- Chunk: {{chunk_name}} ({{chunk_index}} of {{chunk_count}})
- Window: {{start_ts}} – {{end_ts}} ({{start_sec}}–{{end_sec}} seconds, video-absolute)
- Transcript artifact: {{transcript_path}}
  Read ONLY the cues whose times fall inside your window. Do not read any other
  file, directory, or artifact; do not browse the repository; do not use the
  network; do not call any other tool.

TASK
Find the audience-worthy moments inside your window: the disagreements, the
stories, the actionable advice, the surprises — what a viewer would send to a
friend. Times are VIDEO-ABSOLUTE seconds, exactly as the transcript times them
(a moment's `start` is its cue's start; `end` is its cue's end). Every moment
must lie fully inside [{{start_sec}}, {{end_sec}}]. If the window holds nothing
worth clipping, that is a fine answer: an empty list.

OUTPUT CONTRACT
Write ONLY a JSON array of Candidates to {{output_path}} — nothing else, no
markdown fence, no prose, no explanations alongside. Each Candidate:

  {
    "score":  int 0-100,        // how worth-clipping it is; 100 = must-see
    "title": string,            // short, in your own words (max ~80 chars)
    "reason": string,           // 1-2 sentences: why this is worth watching
    "start": number of seconds, // video-absolute, >= {{start_sec}}
    "end":   number of seconds  // video-absolute, <= {{end_sec}}
  }

Rules: 0 <= start < end, both inside the window; `score` an integer; every field
required. The parent validates this file with `python -m aclip.analysis
validate` and caches it with `record`; a file that fails validation is never
cached, and the chunk must be answered again.

**THE FILE IS THE ANSWER — YOUR REPLY IS NOT.** The parent reads
{{output_path}} and never your text. If you print the array in your reply and
write no file, your work is thrown away and the chunk is analysed again from
scratch. Write the file in Turn 2, confirm it in Turn 3, and reply `done`.
**Never paste the array into your reply.** A reply containing the JSON instead
of the file is the one failure mode this prompt exists to prevent.
```

When settings.md `model:` is blank, replace the `Model to reason with` line with:
`Model to reason with: the Coding Agent's own model (you are it — reason over the transcript directly, no other provider)`.

## Why the values are placeholders

`chunk_minutes`, `intro_sec`, `model`, and `language` are settings.md values
(and the window comes from the plan) — the prompt carries no copy of them that
can drift from the input of record. The window boundaries in the prose above
(`{{start_sec}}`/`{{end_sec}}`) are the only numbers the subagent needs to apply
the "fully inside" rule, and they arrive pre-computed from the deterministic
plan, so the subagent never re-derives chunk math. The schema in the prompt is
the same one `python -m aclip.analysis validate` enforces — one contract, stated
once here and checked mechanically there.
