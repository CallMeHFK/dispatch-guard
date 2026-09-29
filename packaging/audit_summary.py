#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Summarize a dispatch_guard.jsonl audit log for the operator.

The warn -> enforce promotion decision ("does the table route what I expect?")
should be made on evidence, not vibes — the same closed loop the agent-
evaluation literature calls for (offline + online evaluation driving
adaptation). This script reads the plugin's JSONL audit trail and reports:

- event counts by action (denied / warned / needs_config / no_owner)
- which agents the hints name, and how often each was trusted
- the deliverable classes that passed with no live owner — the specialist
  gaps: either install one, extend the whitelist, or add deliverable rules
- the most frequent targets, for spotting misrouted classes fast

Stdlib-only; the log records real tool targets, so review output before
sharing it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def summarize(records: list[dict]) -> dict:
    actions = Counter(str(r.get("action", "?")) for r in records)
    modes = Counter(str(r.get("mode", "?")) for r in records)
    hints = Counter(
        str(r.get("message", "")).split("派发给 ")[-1].split("；")[0].split(" ")[0]
        for r in records
        if r.get("action") == "denied"
    )
    no_owner = Counter(str(r.get("target", "?")) for r in records if r.get("action") == "no_owner")
    top_targets = Counter(str(r.get("target", "?")) for r in records)
    return {
        "events": len(records),
        "actions": dict(actions),
        "modes": dict(modes),
        "denied_by_agent": dict(hints),
        "no_owner_targets": dict(no_owner),
        "top_targets": dict(top_targets.most_common(10)),
    }


def format_report(summary: dict) -> str:
    lines = [f"events: {summary['events']}"]
    for section, title in (
        ("actions", "by action"),
        ("modes", "by mode"),
        ("denied_by_agent", "denials naming an agent"),
        ("no_owner_targets", "passed with no live owner (specialist gaps)"),
        ("top_targets", "most frequent targets"),
    ):
        data = summary.get(section) or {}
        if not data:
            continue
        lines.append(f"{title}:")
        for key, count in sorted(data.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count:5d}  {key}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", help="path to dispatch_guard.jsonl")
    args = parser.parse_args()
    path = Path(args.log)
    try:
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError as exc:
        sys.exit(f"cannot read {path}: {exc}")
    except ValueError as exc:
        sys.exit(f"{path} is not valid JSONL: {exc}")
    print(format_report(summarize(records)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
