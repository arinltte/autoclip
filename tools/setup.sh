#!/bin/bash
# AutoClip — fresh-device bootstrap. Run once after `git clone`:
#
#   tools/setup.sh
#
# Idempotent: every step is skipped when its artifact already exists.
# What a fresh clone is missing (all gitignored, all rebuildable):
#   .venv/            the Python environment for the aclip CLI's sidecars
#                     (skills/autoclip-clip/scripts/requirements.txt, exact pins)
#   tools/bin/ffmpeg  static ffmpeg WITH libass/drawtext (Homebrew's is lean)
#   tools/vendor/bgutil-ytdlp-pot-provider   PO-token server (public repo)
#   models/hf-cache   whisper weights (~800 MB, downloaded on first transcribe)
# And what only the human can provide (never committed):
#   .secrets/nvidia_api_key          NVIDIA NIM key (or pi's auth.json)
#   .secrets/youtube_client_secret.json + youtube_token.json  (OAuth once)
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
fail() { echo "SETUP-FAIL: $*" >&2; exit 1; }
step() { echo; echo "== $* =="; }

# --- 0) prerequisites -------------------------------------------------------
step "checking prerequisites"
command -v git >/dev/null || fail "git not found"
PY=3.13
if command -v uv >/dev/null; then
  PKG="uv"
elif command -v pip3 >/dev/null; then
  PKG="pip"
else
  fail "need 'uv' (brew install uv) or python3.13 + pip3"
fi
echo "package manager: $PKG"

# --- 1) .venv (the environment the aclip CLI's sidecars run in) ---------------
step "python environment for the sidecars (.venv, python $PY)"
if [ -x ".venv/bin/python" ]; then
  echo "exists — skipping"
else
  if [ "$PKG" = uv ]; then
    uv venv .venv --python "$PY" || fail "uv venv failed"
  else
    python$PY -m venv .venv || fail "python venv failed"
  fi
fi
if [ "$PKG" = uv ]; then
  uv pip install --python .venv/bin/python -r skills/autoclip-clip/scripts/requirements.txt || fail "dependency install failed"
else
  .venv/bin/python -m pip install -r skills/autoclip-clip/scripts/requirements.txt || fail "dependency install failed"
fi

# --- 2) static ffmpeg (libass + drawtext are REQUIRED for caption rendering) --
step "ffmpeg with text filters (tools/bin/ffmpeg)"

# `ffmpeg -filters | grep -q FILTER` is UNSAFE under `set -o pipefail`: grep -q
# exits on its first match, ffmpeg dies of SIGPIPE, and the pipeline reports
# failure on a build that HAS the filter. Match in the shell instead — no pipe.
ffmpeg_has() { # $1 = binary, $2 = filter name → 0 when the filter is listed
  local filters
  filters="$("$1" -hide_banner -filters 2>/dev/null)" || return 1
  case "$filters" in *"$2"*) return 0 ;; *) return 1 ;; esac
}

mkdir -p tools/bin
need_ffmpeg=1
if [ -x tools/bin/ffmpeg ] && ffmpeg_has tools/bin/ffmpeg drawtext; then
  echo "exists with drawtext — skipping"
  need_ffmpeg=0
fi
if [ "$need_ffmpeg" = 1 ]; then
  # A qualifying SYSTEM ffmpeg is linked in (the gate reads tools/bin/ffmpeg).
  system_ffmpeg="$(command -v ffmpeg || true)"
  if [ -n "$system_ffmpeg" ] && ffmpeg_has "$system_ffmpeg" drawtext \
     && ffmpeg_has "$system_ffmpeg" libass; then
    ln -sf "$system_ffmpeg" tools/bin/ffmpeg
    echo "linked system ffmpeg ($system_ffmpeg)"
    need_ffmpeg=0
  fi
fi
if [ "$need_ffmpeg" = 1 ]; then
  # Static builds per arch; the names live on the site index
  # (https://www.osxexperts.net — verified 2026-10-31). The old guesses
  # FFmpeg6arm64.zip / FFmpeg9arm64.zip both 404 and broke fresh clones.
  case "$(uname -m)" in
    arm64|aarch64) FFMPEG_URL="https://www.osxexperts.net/ffmpeg9arm.zip" ;;
    *) FFMPEG_URL="https://www.osxexperts.net/ffmpeg80intel.zip" ;;
  esac
  TMP="$(mktemp -d)"
  if curl -fsSL -o "$TMP/ffmpeg.zip" "$FFMPEG_URL"; then
    ditto -x -k "$TMP/ffmpeg.zip" "$TMP" 2>/dev/null || unzip -o -q "$TMP/ffmpeg.zip" -d "$TMP"
    found="$(find "$TMP" -name ffmpeg -type f | head -1)"
    if [ -n "$found" ]; then
      cp "$found" tools/bin/ffmpeg && chmod +x tools/bin/ffmpeg
    fi
  fi
  rm -rf "$TMP"
fi
if [ -x tools/bin/ffmpeg ] && ffmpeg_has tools/bin/ffmpeg drawtext; then
  tools/bin/ffmpeg -hide_banner -version | head -1
else
  fail "no ffmpeg with drawtext — caption rendering WILL fail. Install one into
  tools/bin/ffmpeg by hand: a static build from https://www.osxexperts.net
  (the site index lists the current arch-specific zip names) or 'brew install
  ffmpeg' IF its build carries libass + freetype (Homebrew's usually does not)."
fi

# --- 3) bgutil PO-token provider (plugin comes from the skill requirements) ---
step "bgutil PO-token provider server (tools/vendor/…)"
VEND="tools/vendor/bgutil-ytdlp-pot-provider"
if [ -f "$VEND/server/package.json" ]; then
  echo "exists — skipping"
else
  mkdir -p tools/vendor
  # the server must match the pip plugin version pinned in the skill requirements
  git clone --depth 1 --branch 2.0.0 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$VEND" \
    || echo "WARNING: clone failed — >360p downloads may 403. See $VEND upstream." >&2
fi
if [ -d "$VEND/server" ]; then
  if command -v deno >/dev/null; then
    (cd "$VEND/server" && deno install --allow-scripts=npm:canvas --frozen) \
      || (cd "$VEND/server" && npm ci) || echo "WARNING: server deps install failed" >&2
  elif command -v npm >/dev/null; then
    (cd "$VEND/server" && npm ci) || echo "WARNING: server deps install failed" >&2
  else
    echo "WARNING: neither deno nor npm found — install one for >360p downloads" >&2
  fi
fi

# --- 4) secrets checklist (never printed, never committed) -------------------
step "secrets checklist (.secrets/ — never committed, never printed)"
missing=""
[ -f .secrets/nvidia_api_key ] || { missing="$missing\n  - .secrets/nvidia_api_key  (NVIDIA NIM key — analysis)"; }
[ -f .secrets/youtube_client_secret.json ] || { missing="$missing\n  - .secrets/youtube_client_secret.json  (Google OAuth client, Desktop app)"; }
[ -f .secrets/youtube_token.json ] || { missing="$missing\n  - .secrets/youtube_token.json  (created by: .venv/bin/python tools/youtube_upload.py --check)"; }
if [ -z "$missing" ]; then
  echo "all three present"
else
  echo "still missing (the GUI/env.sh also reads NVIDIA from ~/.pi/agent/auth.json):$missing"
fi

# --- 5) gate — the skill set's own doctor ------------------------------------
step "gate (aclip doctor + the pytest suite)"
# Run from PROJECT_DIR (the script's top cd) — inside the scripts dir the
# venv python is ../../../.venv/bin/python, three levels up, not one.
PYTHONPATH=skills/autoclip-clip/scripts .venv/bin/python -m aclip doctor \
  || fail "doctor is red — the FAIL line names the step above that repairs it"
.venv/bin/python -m pytest skills/autoclip-clip/scripts/tests -q || fail "test suite red"

echo
echo "SETUP-OK — hand a YouTube link to the autoclip-clip skill."
echo "The first run downloads the whisper model (~800 MB) into models/hf-cache automatically."
