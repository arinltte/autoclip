"""``aclip`` argument parsing: one CLI, one subcommand per pipeline stage —
all wired. doctor/transcript-fetch/compare/transcribe/scaffold/download/render
dispatch to their modules; every stage of the pipeline has exactly one entry
point here (spec decision 3).
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from . import compare, download, doctor, render, scaffold, transcribe, transcript_fetch

# Every subcommand is now wired: doctor (#24), transcript-fetch (#25), compare (#26),
# transcribe (#27), scaffold + download (#28), render (#29).


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``aclip`` parser and its subcommands."""
    parser = argparse.ArgumentParser(
        prog="aclip",
        description="AutoClip CLI: one entry point for the skills pipeline.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    subparsers.add_parser("doctor", help="check the project environment and report what is missing")
    fetch_parser = subparsers.add_parser(
        "transcript-fetch", help="fetch the YouTube Track into the Project (ticket #25)"
    )
    fetch_parser.add_argument("video_id", help="the YouTube Video Id (the Project's dir name)")
    compare_parser = subparsers.add_parser(
        "compare", help="compare both tracks and write the divergence report (ticket #26)"
    )
    compare_parser.add_argument("video_id", help="the YouTube Video Id (the Project's dir name)")
    transcribe_parser = subparsers.add_parser(
        "transcribe", help="run the Whisper Track into the Project (ticket #27)"
    )
    transcribe_parser.add_argument("video_id", help="the YouTube Video Id (the Project's dir name)")
    transcribe_parser.add_argument(
        "--model", default=None, help="the whisper model repo (default: the settings model, then large-v3-turbo)"
    )
    transcribe_parser.add_argument(
        "--language", default=None, help="the spoken language ISO code (default: settings.md language)"
    )
    scaffold_parser = subparsers.add_parser("scaffold", help="create the Project's folders (ticket #28)")
    scaffold_parser.add_argument("video_id", help="the YouTube Video Id (the Project's dir name)")
    download_parser = subparsers.add_parser(
        "download", help="download the media through the three-rung ladder (ticket #28)"
    )
    download_parser.add_argument("source", help="the YouTube URL or a local media file")
    download_parser.add_argument("--video-id", default=None, help="the Project's Video Id (default: from the source)")
    render_parser = subparsers.add_parser(
        "render", help="render the selection's clips concurrently (ticket #29)"
    )
    render_parser.add_argument("video_id", help="the YouTube Video Id (the Project's dir name)")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch ``aclip`` and return the process exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    if args.command == "doctor":
        return doctor.main()
    if args.command == "transcript-fetch":
        return transcript_fetch.main([args.video_id])
    if args.command == "compare":
        return compare.main([args.video_id])
    if args.command == "scaffold":
        return scaffold.main([args.video_id])
    if args.command == "download":
        forwarded = [args.source]
        if args.video_id:
            forwarded += ["--video-id", args.video_id]
        return download.main(forwarded)
    if args.command == "transcribe":
        forwarded = [args.video_id]
        if args.model:
            forwarded += ["--model", args.model]
        if args.language:
            forwarded += ["--language", args.language]
        return transcribe.main(forwarded)
    if args.command == "render":
        return render.main([args.video_id])
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    sys.exit(main())
