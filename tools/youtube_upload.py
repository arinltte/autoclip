#!/usr/bin/env python3
# =============================================================================
# YouTube uploader (Data API v3, OAuth 2.0, resumable upload)
#
# One-time setup:
#   1. https://console.cloud.google.com  -> create/choose a project
#   2. Enable "YouTube Data API v3" for that project
#   3. OAuth consent screen -> External -> add your Google account as a test user
#   4. Credentials -> Create credentials -> OAuth client ID -> Desktop app
#      -> download the JSON -> save as  .secrets/youtube_client_secret.json
#   5. .venv/bin/python tools/youtube_upload.py --check
#      (browser opens once for consent; refresh token cached in .secrets/youtube_token.json)
#
# Usage:
#   tools/youtube_upload.py --check
#   tools/youtube_upload.py --file clip.mp4 --title "My Short" \
#       [--description "..."] [--tags a,b] [--privacy public|unlisted|private]
#       [--publish-at "2025-01-20 19:00"]        # local time -> becomes a scheduled post
#
# Notes:
#   - Uploads cost 1600 quota units each; default project quota 10,000/day (~6 uploads).
#   - API projects that have not passed Google's OAuth audit upload fine but the
#     videos are LOCKED PRIVATE until the (free) audit is completed. See README.
#   - Scheduling uses privacyStatus=private + publishAt (RFC3339 UTC).
# =============================================================================
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SECRET = PROJECT_DIR / ".secrets" / "youtube_client_secret.json"
DEFAULT_TOKEN = PROJECT_DIR / ".secrets" / "youtube_token.json"
SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"  # needed for channels.list(mine=True)
SCOPE_FULL = "https://www.googleapis.com/auth/youtube"
SCOPES = [SCOPE_UPLOAD, SCOPE_READONLY]
FFPROBE = os.environ.get("FFPROBE_BIN") or "ffprobe"


class UploadError(RuntimeError):
    """An upload failure. ``reason`` carries the API error's machine reason
    (``uploadLimitExceeded`` / ``quotaExceeded`` / …) when the failure came
    from the API — the structured input of :func:`classify`, so callers never
    string-match the humanized message."""

    def __init__(self, msg, reason: str | None = None):
        super().__init__(msg)
        self.reason = reason or None


# ---------------------------------------------------------------- helpers ---

def find_client_secret(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise UploadError(f"client secret not found: {p}")
        return p
    env = os.environ.get("GOOGLE_OAUTH_CLIENT")
    if env and Path(env).expanduser().exists():
        return Path(env).expanduser()
    if DEFAULT_SECRET.exists():
        return DEFAULT_SECRET
    hits = sorted(glob.glob(str(PROJECT_DIR / ".secrets" / "client_secret*.json")))
    if hits:
        return Path(hits[0])
    raise UploadError(
        "No OAuth client secret found.\n"
        "  1. https://console.cloud.google.com -> enable 'YouTube Data API v3'\n"
        "  2. OAuth consent screen -> External -> add your account as a test user\n"
        "  3. Credentials -> Create OAuth client ID -> Desktop app -> download JSON\n"
        f"  4. save it as {DEFAULT_SECRET}"
    )


def get_credentials(client_secret: Path, token_file: Path,
                    scopes: list[str] | None = None) -> Credentials:
    scopes = scopes or SCOPES
    creds: Credentials | None = None
    if token_file.exists():
        try:
            # NOTE: from_authorized_user_file(path, scopes) OVERRIDES the stored scopes,
            # so the stored scopes must be checked from the raw JSON — otherwise a
            # token granted fewer scopes would silently pass the check and 403 later.
            stored = set(json.loads(token_file.read_text(encoding="utf-8")).get("scopes") or [])
            if not set(scopes) <= stored:
                print("! saved token lacks required scopes; re-authenticating", file=sys.stderr)
            else:
                creds = Credentials.from_authorized_user_file(str(token_file))
        except Exception as e:  # corrupted
            print(f"! token file unusable ({e}); re-authenticating", file=sys.stderr)
            creds = None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            print(f"! token refresh failed ({e}); re-authenticating", file=sys.stderr)
            creds = None
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), scopes)
        creds = flow.run_local_server(port=0, prompt="consent")
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
        os.chmod(token_file, 0o600)
        print(f"✓ OAuth token saved -> {token_file}", file=sys.stderr)
    return creds


def probe_duration(path: str | Path) -> float | None:
    """Exact final duration (seconds) of a rendered file via ffprobe."""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:
        return None


def http_reason(err) -> str:
    """The API error's machine ``reason`` (``uploadLimitExceeded``,
    ``quotaExceeded``, …) from an HttpError's JSON body; ``""`` when absent."""
    try:
        data = json.loads(err.content or b"{}")
        errs = data.get("error", {}).get("errors", [{}])
        return str(errs[0].get("reason", "") or "")
    except Exception:  # noqa: BLE001 — an unparseable body has no reason
        return ""


# The structured classes of the API error reasons (NOTES §5.2): ``cap`` is the
# per-CHANNEL daily upload limit — distinct from the per-PROJECT Data API
# ``quota`` (an upload costs 1600 of 10,000 units/day; the cap's real reset is
# undocumented — the Tally measures, never guesses).
ERROR_CLASSES = {
    "uploadLimitExceeded": "cap",
    "quotaExceeded": "quota",
    "authError": "auth",
}


def classify(err) -> str:
    """The structured class of an upload failure: ``cap`` | ``quota`` | ``auth``
    | ``other`` (NOTES §5.2). Accepts an HttpError, an UploadError carrying its
    API ``reason`` (what :func:`upload_video` raises), or a bare reason string;
    anything unrecognised is ``other``.
    """
    if isinstance(err, str):
        return ERROR_CLASSES.get(err.strip(), "other")
    reason = ""
    if isinstance(err, HttpError):
        # HttpError's own ``reason`` attribute is the HTTP transport phrase
        # ("Ok"/"Forbidden"), not the API reason — parse the JSON body first.
        reason = http_reason(err)
        if not reason and str(getattr(err.resp, "status", "")) == "401":
            return "auth"
    if not reason:
        reason = getattr(err, "reason", None) or ""
    return ERROR_CLASSES.get(str(reason).strip(), "other")


def humanize_http_error(err: HttpError) -> str:
    reason, message = "", ""
    try:
        data = json.loads(err.content or b"{}")
        errs = data.get("error", {}).get("errors", [{}])
        reason = errs[0].get("reason", "")
        message = data.get("error", {}).get("message", "")
    except Exception:
        message = str(err)
    if reason == "quotaExceeded":
        return ("YouTube API quota exhausted (an upload costs 1600 units; default is "
                "10,000/day). Quota resets at midnight Pacific Time.")
    if reason == "uploadLimitExceeded":
        return "The channel has reached its daily video upload limit."
    if reason == "invalidPublishAt" or (reason == "invalidValue" and "publishAt" in message):
        return "publishAt must be at least ~15 minutes in the future (RFC3339)."
    if reason == "forbidden":
        return f"Forbidden by YouTube ({message}). Check channel standing and API project."
    if reason == "drmNotAllowed" or reason == "invalidVideoFile":
        return f"YouTube rejected the file ({message})."
    return f"YouTube API error ({err.resp.status} {reason or 'unknown'}): {message}"


def to_rfc3339_utc(local_dt: datetime) -> str:
    if local_dt.tzinfo is None:
        local_dt = local_dt.astimezone()  # interpret naive as local time
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- actions ---

def check_auth(client_secret: str | None, token_file: str | None) -> None:
    creds = get_credentials(find_client_secret(client_secret),
                            Path(token_file) if token_file else DEFAULT_TOKEN)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    resp = yt.channels().list(part="snippet,contentDetails", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise UploadError("Authenticated, but no YouTube channel is attached to this account.")
    ch = items[0]["snippet"]
    print(f"✓ authenticated as channel: {ch['title']}  (id: {items[0]['id']})")
    print(f"  https://www.youtube.com/channel/{items[0]['id']}")


def upload_video(file: str, *, title: str, description: str = "", tags: list[str] | None = None,
                 category_id: str = "22", privacy: str = "public",
                 publish_at: str | None = None, made_for_kids: bool = False,
                 client_secret: str | None = None, token_file: str | None = None,
                 scopes: list[str] | None = None, on_progress=None) -> dict:
    """Upload one file. publish_at: RFC3339 UTC string -> becomes a scheduled post.

    Returns dict with video_id / url / privacy / publish_at.
    """
    path = Path(file).expanduser()
    if not path.exists():
        raise UploadError(f"file not found: {path}")
    if not path.suffix.lower().endswith((".mp4", ".mov", ".m4v", ".webm")):
        raise UploadError(f"unexpected video container: {path.suffix}")

    clean_title = " ".join(str(title).split())[:100]
    if not clean_title:
        raise UploadError("title is empty")

    status: dict = {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(made_for_kids)}
    if publish_at:
        # YouTube requires a scheduled video to be private with publishAt set.
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at

    body = {
        "snippet": {
            "title": clean_title,
            "description": (description or "")[:4900],
            "tags": [t for t in (tags or []) if t],
            "categoryId": str(category_id),
        },
        "status": status,
    }

    creds = get_credentials(find_client_secret(client_secret),
                            Path(token_file) if token_file else DEFAULT_TOKEN, scopes)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)

    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"  uploading {path.name} ({size_mb:.1f} MB, -> {status['privacyStatus']}"
          f"{' @ ' + publish_at if publish_at else ''}) …", flush=True)

    media = MediaFileUpload(str(path), mimetype="video/mp4",
                            chunksize=8 * 1024 * 1024, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media,
                                 notifySubscribers=True)
    response = None
    last_pct = -1
    while response is None:
        try:
            progress, response = request.next_chunk(num_retries=5)
        except HttpError as e:
            raise UploadError(humanize_http_error(e), reason=http_reason(e)) from e
        if progress is not None and response is None:
            pct = int(progress.progress() * 100)
            if pct != last_pct and pct % 10 == 0:
                print(f"    {pct}%", flush=True)
                last_pct = pct
            if on_progress:
                on_progress(pct)

    vid = response["id"]
    return {
        "video_id": vid,
        "url": f"https://youtu.be/{vid}",
        "shorts_url": f"https://www.youtube.com/shorts/{vid}",
        "title": response.get("snippet", {}).get("title", clean_title),
        "privacy": response.get("status", {}).get("privacyStatus", status["privacyStatus"]),
        "publish_at": response.get("status", {}).get("publishAt", publish_at),
    }


# -------------------------------------------------------------------- CLI ---

def main() -> None:
    ap = argparse.ArgumentParser(description="Upload a video to YouTube (Data API v3).")
    ap.add_argument("--check", action="store_true", help="verify OAuth login, print channel")
    ap.add_argument("--file", help="video file to upload")
    ap.add_argument("--title")
    ap.add_argument("--description", default="")
    ap.add_argument("--tags", default="", help="comma-separated tags")
    ap.add_argument("--category-id", default="22",
                    help="22 People&Blogs, 27 Education, 20 Gaming, …")
    ap.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"])
    ap.add_argument("--publish-at", default=None,
                    help="local time, e.g. '2025-01-20 19:00' or '19:00' (today/tomorrow)")
    ap.add_argument("--made-for-kids", action="store_true")
    ap.add_argument("--client-secret", default=None)
    ap.add_argument("--token-file", default=None)
    args = ap.parse_args()

    if args.check:
        check_auth(args.client_secret, args.token_file)
        return

    if not args.file or not args.title:
        ap.error("--file and --title are required (or use --check)")

    publish_at = None
    if args.publish_at:
        raw = args.publish_at.strip()
        if "T" not in raw and " " not in raw:  # bare "19:00" -> today or tomorrow
            hh, mm = map(int, raw.split(":"))
            now = datetime.now().astimezone()
            cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand <= now:
                cand += __import__("datetime").timedelta(days=1)
            raw = cand.isoformat()
        publish_at = to_rfc3339_utc(datetime.fromisoformat(raw.replace("T", " ")))
        if args.privacy == "public":
            print("note: scheduled posts are private with a publishAt date "
                  "(YouTube flips them public automatically)", file=sys.stderr)

    res = upload_video(args.file, title=args.title, description=args.description,
                       tags=[t.strip() for t in args.tags.split(",") if t.strip()],
                       category_id=args.category_id, privacy=args.privacy,
                       publish_at=publish_at, made_for_kids=args.made_for_kids,
                       client_secret=args.client_secret, token_file=args.token_file)
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (UploadError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
