#!/bin/bash
# Shared environment for all tools. Source this: `source "$(dirname "$0")/env.sh"`
# Keeps ALL model downloads and runtime data inside this project folder.

# Project root = the folder containing this script's parent (works on any
# machine/path — a git clone anywhere resolves to itself).
export PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# HuggingFace cache -> project-local (model download requirement)
export HF_HUB_CACHE="$PROJECT_DIR/models/hf-cache"
export HF_HOME="$PROJECT_DIR/models/hf"

# AutoClip runtime data (sqlite, logs, cli-settings) -> project-local
export AUTOCLIP_DATA_DIR="$PROJECT_DIR/data"

# NVIDIA NIM API key (never commit / never print)
# Priority: project .secrets file -> pi coding agent auth.json (~/.pi/agent/auth.json)
if [ ! -f "$PROJECT_DIR/.secrets/nvidia_api_key" ] && [ -f "$HOME/.pi/agent/auth.json" ]; then
  .venv/bin/python -c "import json,sys;print(json.load(open('$HOME/.pi/agent/auth.json')).get('nvidia',{}).get('key',''))" > "$PROJECT_DIR/.secrets/nvidia_api_key" 2>/dev/null
fi
if [ -f "$PROJECT_DIR/.secrets/nvidia_api_key" ]; then
  export NVIDIA_API_KEY="$(cat "$PROJECT_DIR/.secrets/nvidia_api_key")"
  export AUTOCLIP_API_KEY="$(cat "$PROJECT_DIR/.secrets/nvidia_api_key")"
  export OPENAI_API_KEY="$(cat "$PROJECT_DIR/.secrets/nvidia_api_key")"
fi

# LLM used for analysis/clipping, on NVIDIA NIM only (AGENTS.md hard rule 1 —
# no third-party gateways; OpenRouter ran out of credits, HTTP 402).
#
# Chain history (all measured on real ~6-min transcript chunks, NVIDIA NIM;
# the Rust analyzer, src/analyze.rs, runs the same chain):
#   17 Oct  glm-5.3-flash reasons unboundedly (burns 6-16K tokens without
#           answering; requests with max_tokens>=8192 hang 15+ min) and glm-5.3
#           times out even on trivial prompts. gemma-4-31b-it answered a full
#           chunk in ~1-2 min with clean JSON and became the fallback.
#   20 Oct  stepfun-ai/step-5-preview: at the 4096 default every chunk returned
#           finish_reason='length' with EMPTY content — it spends ~9K
#           completion tokens on reasoning that NIM reports as
#           reasoning_tokens=0 before answering. With a 16384 cap it answers
#           cleanly (~205s a chunk). It became the primary; gemma was dropped
#           from the chain and z-ai/glm-5.3 stayed as backup.
# A fresh/config-less run ships a PRIMARY-ONLY bootstrap (spec "Default chain
# for fresh installs" + ADR 0003): the Model Chain is one attempt per model per
# chunk with error-triggered failover (ADR 0005), and backups are a deliberate
# addition, not a shipped default — so LLM_BACKUP_MODEL defaults to empty.
# Keep this bootstrap in sync with the Rust defaults (src/app_config.rs DEFAULTS,
# src/sidecar.rs DEFAULT_LLM_* and src/model_chain.rs resolve_chain's env trio)
# — a stale non-empty entry re-adds a backup to a config-less run.
export LLM_BASE_URL="${LLM_BASE_URL:-https://integrate.api.nvidia.com/v1}"
export LLM_MODEL="${LLM_MODEL:-stepfun-ai/step-5-preview}"
export LLM_BACKUP_MODEL="${LLM_BACKUP_MODEL:-}"
export LLM_FALLBACK_MODEL="${LLM_FALLBACK_MODEL:-}"
export LLM_API_KEY="$NVIDIA_API_KEY"

# Disable extended thinking on NIM reasoning models (prevents HTTP 504 on large
# extraction tasks; see PATCH-1 in engine/backend/core/llm_providers.py)
export AUTOCLIP_LLM_EXTRA_BODY='{"chat_template_kwargs": {"thinking": false}}'

# Whisper model used for transcription (mlx-community, Apple Silicon MLX)
export WHISPER_MODEL="${WHISPER_MODEL:-mlx-community/whisper-large-v3-turbo}"

# ffmpeg: static full-featured build (tools/bin/ffmpeg, ffmpeg 9.0 arm64).
# Homebrew's ffmpeg 8.1.2 is a LEAN build without libass/freetype -> no
# drawtext/subtitles/ass filters (text rendering impossible). The static
# build has all of them. tools/setup.sh fetches it; a system ffmpeg with
# drawtext+libass also works (checked at runtime).
export FFMPEG_BIN="${FFMPEG_BIN:-$PROJECT_DIR/tools/bin/ffmpeg}"
if [ ! -x "$FFMPEG_BIN" ]; then
  FFMPEG_BIN="$(command -v ffmpeg || true)"
fi
export FFPROBE_BIN="${FFPROBE_BIN:-$(command -v ffprobe || echo /opt/homebrew/bin/ffprobe)}"
export AUTOCLIP_FFMPEG_PATH="$FFMPEG_BIN"
export PATH="$PROJECT_DIR/tools/bin:$PATH"
