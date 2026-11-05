"""``aclip scaffold`` — one Project's folders (ticket #28).

The frozen tree's runtime state: ``projects/<Video Id>/{download,transcript,
analyze,output}``. Idempotent (a re-run costs nothing), it creates only the
folders, never media.
"""

from __future__ import annotations

import sys
from pathlib import Path

from aclip.transcript_fetch import project_dir_for  # the one spelling of projects/<id>

PROJECT_SUBDIRS = ("download", "transcript", "analyze", "output")


def scaffold_project(video_id: str) -> Path:
    """Create the Project's four folders; return its directory."""
    project_dir = project_dir_for(video_id)
    for subdir in PROJECT_SUBDIRS:
        (project_dir / subdir).mkdir(parents=True, exist_ok=True)
    return project_dir


def main(argv=None) -> int:
    """``aclip scaffold <Video Id>`` — prints the Project directory."""
    argv = list(sys.argv[2:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: aclip scaffold <Video Id>", file=sys.stderr)
        return 0 if argv and argv[0] in {"-h", "--help"} else 2
    project_dir = scaffold_project(argv[0])
    for subdir in PROJECT_SUBDIRS:
        print(project_dir / subdir)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    sys.exit(main())
