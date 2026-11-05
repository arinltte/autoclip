# settings.md — AutoClip analysis & render settings

The tracked default the skill set reads. Hand-edit the plain `key: value`
lines below; grouped by pipeline stage, each with a short plain-language gloss.

## Analysis
model: — the Coding Agent's own model; leave blank unless you need an explicit override
language: en — the spoken language to transcribe; never "auto" (whisper once misdetected an English podcast as Hindi)
similarity_threshold: 0.85 — how alike two moments must be to count as the same; calibrated on the real X5HIcE2BRuc pair, never change
prefer: whisper — which track's wording the captions use: whisper / youtube / both; the compare step records your answer here when the two tracks disagree, and never asks twice
parallelism: 1 — analysis chunks run at once; keep at 1 (the NVIDIA 40 requests/minute rule)
chunk_minutes: 6 — audio is split into chunks this many minutes long for analysis
intro_sec: 90 — the first 90 seconds are treated as intro and skipped when hunting for clips

## Curation
clip_min_sec: 30 — the shortest clip we will output, in seconds
clip_max_sec: 90 — the longest clip we will output, in seconds
target_count: 10 — how many clips to aim for per project
min_score: none — no quality-score floor (leave unset)

## Render
speed: 1.3 — the playback-speed CEILING, not a default: a clip that already fits the platform's 60 s renders at 1.0, and a longer one takes the smallest step up (1.1, 1.2, 1.25, …) that fits it, never more than this
title_card: enabled=true, duration_sec=6, fontsize=46, box=true, box_opacity=0.45, font= (empty = bundled default), fontcolor=#ffffff, kicker=HIGHLIGHT, kicker_color=#ffd24a, max_lines=2, padding_pct=6, min_fontscale=0.7, max_chars= (blank = derive from the padding) — the intro title-card overlay shown at the top of each clip; a long title wraps to at most max_lines lines, keeps padding_pct of the frame clear of text, and the font shrinks to min_fontscale before a third line is used
captions: enabled=true, style=karaoke, fontsize=54, spoken_color=#ffff00, unspoken_color=#ffffff, outline=2, margin_v=420 — the burned-in captions (karaoke highlights the spoken word)

`model` and `parallelism` are agent-only keys (the Coding Agent sets them); every other key is safe to hand-edit.
