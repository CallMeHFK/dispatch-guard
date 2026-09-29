#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Print the changelog section for a release tag, for use as release notes."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

FALLBACK = "See `docs/CHANGELOG.md` for {tag}."


def extract(changelog: Path, tag: str) -> tuple[str, bool]:
    """Return (section text, found).

    The changelog canon is `## vX.Y.Z` (optionally with a date suffix). `###` is
    accepted too so an older heading level cannot silently fall back to the
    placeholder note — that mismatch shipped once as a real bug: the changelog
    said `##`, the extractor looked for `###`, and every release page would have
    carried the "See docs/CHANGELOG.md" filler instead of the actual notes.
    """
    heading = re.compile(rf"^#{{2,3}}\s+{re.escape(tag)}(?:\s|$)")
    try:
        lines = changelog.read_text(encoding="utf-8").splitlines()
    except OSError:
        return FALLBACK.format(tag=tag), False
    for index, line in enumerate(lines):
        if not heading.match(line):
            continue
        notes: list[str] = []
        for body in lines[index + 1:]:
            if body.startswith("##"):
                break
            notes.append(body)
        text = "\n".join(notes).strip()
        if text:
            return text, True
        break
    return FALLBACK.format(tag=tag), False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, e.g. v1.4.7")
    parser.add_argument(
        "--changelog",
        default=str(REPO / "docs" / "CHANGELOG.md"),
        help="changelog to read (default: docs/CHANGELOG.md)",
    )
    args = parser.parse_args()

    text, found = extract(Path(args.changelog), args.tag)
    if not found:
        sys.stderr.write(f"no {args.tag} section in {args.changelog}\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
