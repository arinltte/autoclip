# Analysis spawn — recovery

Reached from SKILL.md step 6 when a spawn misbehaves. **None of this is one of
the four interrupts**, none of it stops the run, and none of it ever means
running two children at once. The spawn's shape is in SKILL.md step 6; the prompt
the child receives is `analysis-prompt.md`.

## The rule that governs every branch below

The account lockout that costs a day comes from a **task-less child that loops** —
a child launched with no mission that starts exploring the repository and burns
the 40-requests/minute quota. The bounded preamble in `analysis-prompt.md`
(at most 3 tool turns, one named file, nothing else) is what prevents that, and
it is why re-spawning the same chunk is safe where improvising is not.

So the recovery posture is fixed: **same chunk, same task, one at a time, back
off before you retry.** Never widen the search, never swap agents, never run a
second child to "catch up".

---

## Branch 1 — a 429, or the spawn call was rejected

- **A spawn that came back with no `task` outranks every other branch.** Check
  the arguments pi echoes in the spawn result; an absent or empty `task` means
  the child received no mission. Do NOT let it run, and do NOT keep its array
  even when it later returns a plausible one — a task-less child "succeeds" only
  by reading the repo and running the plan, which is the very quota-burning
  runaway this file exists to prevent (the 09Dl5rO6TlY run fired nine of them;
  two children tripped real 429s and one ran to a 90-minute timeout). Re-issue
  the **same chunk** at once with the prompt as `task` — verbatim, changed only
  by the instruction's presence. It is one cheap call, and it is the only fix: a
  re-spawn of a task-less child returns the same task-less reply.
- **429 Too Many Requests** is a traffic problem, not a pipeline defect. Wait
  60–90 s with no traffic (`sleep 90`), then re-spawn the **same chunk** with an
  identical task.
- Two 429s in a row → wait another 90 s. If 429s persist across roughly five
  minutes of backoff, stop and quote the exact HTTP status. That is interrupt
  #1 (preflight/model unavailable), and it is the only 429 outcome that ends a
  run.
- **A rejected call** is a schema fault in the call itself, not the model: an
  invalid option key (`parallelism` is the one that trips this) or a bad agent
  name. Fix the call and re-spawn the same chunk. `delegate` is the sanctioned
  child.

## Branch 2 — the child's report is empty, or it only says `done`, but the answer file exists

This is **normal and has been observed in real runs** (two of eight chunks in one
project returned nothing but `done`; one returned a 0-byte file). The report file
is scratch and the child's `reply done` is a complete answer; the answer file is
the contract, not the child's prose.

- Validate `analyze/chunk_NNN.candidates.json`.
- It passes → record it. The chunk is finished — move straight to the next one.
  **Do not re-spawn it**, and do not treat a quiet child as a failure.

## Branch 2b — the answer file is MISSING but the report holds a complete JSON array

**This is the common failure of this model and it is NOT a defect.** In the
reviewed run (#38) four of seven children answered correctly and printed the
array in their reply instead of writing `analyze/chunk_NNN.candidates.json`.
The analysis is done; only the write is missing, and the write is the parent's
mechanical step.

- Copy the array out of `analyze/.chunk_NNN.result.md` — from the first `[` to
  the last `]` — and install it at the answer path. **Add nothing, drop
  nothing, reword nothing**: you are relocating the child's answer, not writing
  your own.
- Run the same gate as every other chunk: `validate`, then `record`.
- Note the reconstruction in the end-of-run report (the reviewed run did, and
  that note is what surfaced the pattern).
- **Never re-spawn for this.** A re-spawn costs a model call on a
  40-requests/minute key and returns the same array as prose again.

A report with a *truncated* or *partial* array is Branch 3, not this branch —
an incomplete answer was never an answer.

## Branch 3 — the answer file is missing AND the report holds no array, or it fails validation

- **Missing with nothing to recover**: re-spawn the same chunk once, with the
  identical task. A child that reported nothing and wrote nothing made no turns
  of progress; the same prompt usually lands.
- **Fails validation**: re-spawn the same chunk once, appending the validator's
  violations verbatim — *"Your previous answer was rejected for: `<violations>`.
  Write only a conforming JSON array to the same path."* The schema is the same
  one printed in the prompt, so quote the failures back rather than re-explaining.
- **One re-spawn per chunk per fault.** A second failure on the same chunk stops
  the run and reports the exact validator output — that is a defect worth a
  human, not a third guess.

## Branch 4 — the chunk already recorded

- The plan lists it under `cache_hits`. It is cached and finished. **Never spawn
  it again**, whatever a report or a suspicion says.
- To settle any doubt about coverage, re-run
  `python -m aclip.analysis plan projects/<Video Id>` — zero model calls — and
  read `cache_hits` / `to_query`. A finished run reports `cache_hits` equal to
  the chunk count and `to_query: []`.

## Branch 5 — a runaway child (the task-less symptom)

A task-**bearing** child is bounded by its prompt (at most 3 turns, one file)
and finishes in minutes, so a child still alive well past ~10 minutes is almost
certainly task-less — it never got the preamble and is exploring. Do not steer
it (that doubles the spend). If it eventually hands back a file, that file was
produced without a mission and is NOT trusted by default: prefer a clean
re-spawn WITH `task` (Branch 1). Only as a last resort — never the first choice
— may you salvage it, and then only through the full gate: `validate` **and** an
explicit window-bounds check against its chunk's `start_sec`/`end_sec` before
`record`, noted as a salvage in the report. The runaway is prevented upstream by
the spawn guard; this branch only bounds the cost when that guard missed one.

## One anti-pattern worth naming

Do not steer a child that is already running to rescue it. Steering after the
child has spent minutes wastes the time it already burned and the model calls it
already made; a clean re-spawn against the same prompt is cheaper. Reserve
steering for a child that has been alive well under a few minutes.
