#!/usr/bin/env python3
"""Print a pi session log (.jsonl) in reading order.

The read tool truncates a log at about 50 KB — half of a real session — and the
agent's private thinking (the part a session review needs most) sits past that
limit. This script prints the whole log: every assistant turn (thinking +
text), every tool call with its arguments, every tool result, and a summary.

Usage:
    python3 tools/read_session.py <session.jsonl> [--max-chars N] [--tail N]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone


def load(path: pathlib.Path) -> list[dict]:
    """The .jsonl rows, in file order. A bad line becomes a parse-error row so
    one torn write never hides the rest of the log."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                rows.append({"type": "parse-error", "lineno": lineno, "error": str(exc)})
    return rows


def _ms(value) -> float | None:
    """A row's timestamp as epoch milliseconds. Two formats exist: epoch
    milliseconds (older logs) and ISO-8601 strings (newer logs). Anything else
    gives None so one odd row never stops the print."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return None
    return None


def _stamp(obj: dict) -> str:
    ms = _ms(obj.get("timestamp"))
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"… [+{len(text) - limit} chars]"


def _error(obj: dict) -> str | None:
    """A row's error, if any. Two shapes exist: the message carries it, or the
    row does."""
    msg = obj.get("message") or {}
    return obj.get("errorMessage") or (msg.get("errorMessage") if isinstance(msg, dict) else None)


def _emit(label: str, text: str, limit: int) -> None:
    print("   " + label + " " + _clip(text, limit).replace("\n", "\n   "))


def print_event(index: int, obj: dict, limit: int) -> None:
    kind = obj.get("type", "?")
    stamp = _stamp(obj)
    when = f" [{stamp}]" if stamp else ""
    if kind == "session":
        print(f"--- {index} SESSION cwd={obj.get('cwd')}{when}")
    elif kind == "model_change":
        print(f"--- {index} MODEL {obj.get('provider')}/{obj.get('modelId')}{when}")
    elif kind == "thinking_level_change":
        print(f"--- {index} THINKING-LEVEL {obj.get('thinkingLevel')}{when}")
    elif kind == "message":
        msg = obj.get("message", {})
        role = msg.get("role", "?")
        err = _error(obj)
        print(f"--- {index} [{role}] err={err}{when}")
        content = msg.get("content")
        if isinstance(content, str):
            _emit("STR:", content, limit * 2)
        elif isinstance(content, list):
            for part in content:
                ptype = part.get("type")
                if ptype == "text":
                    _emit("TEXT:", part.get("text", ""), limit)
                elif ptype == "thinking":
                    _emit("THINK:", part.get("thinking", ""), limit)
                elif ptype == "toolCall":
                    args = json.dumps(part.get("arguments", {}), ensure_ascii=False)
                    _emit(f"TOOLCALL {part.get('name')}:", args, limit)
                else:
                    print(f"   PART {ptype}")
    elif kind == "toolResult":
        parts = [p.get("text", "") for p in (obj.get("content") or []) if isinstance(p, dict)]
        flag = " ERROR" if obj.get("isError") else ""
        print(f"--- {index} RESULT {obj.get('toolName')}{flag}{when}")
        _emit("", " ".join(parts), limit)
    elif kind == "context_edit":
        print(f"--- {index} context-edit{when} (a turn the harness replaced)")
    else:
        print(f"--- {index} {kind} keys={list(obj.keys())}{when}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print a pi session log in full.")
    ap.add_argument("path", help="the .jsonl session log")
    ap.add_argument("--max-chars", type=int, default=2500,
                    help="characters printed per part (default: 2500; 0 = no limit)")
    ap.add_argument("--tail", type=int, default=0,
                    help="print only the last N events")
    args = ap.parse_args(argv)
    path = pathlib.Path(args.path)
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    rows = load(path)
    if args.tail:
        rows = rows[-args.tail:]

    assistant = errors = 0
    first = last = None
    for obj in rows:
        if obj.get("type") == "message" and obj.get("message", {}).get("role") == "assistant":
            if _error(obj):
                errors += 1
            else:
                assistant += 1
        ms = _ms(obj.get("timestamp"))
        if ms is not None:
            first = ms if first is None else first
            last = ms if last is None or ms > last else last

    limit = 0 if args.max_chars == 0 else args.max_chars
    for index, obj in enumerate(rows, 1):
        print_event(index, obj, limit)

    minutes = round((last - first) / 60000, 1) if first and last else 0.0
    print(f"\n== summary: {len(rows)} events, {assistant} assistant turns, "
          f"{errors} errored turns, {minutes} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
