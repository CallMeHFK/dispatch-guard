#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The published plugin archive and the repository must never carry a real table.

dispatch-guard's routes.json holds the operator's private dispatch shape: agent
ids, internal system keywords, directory conventions. The repository ships
``routes.example.json`` only, and the build refuses to package anything else.
"""
import subprocess
import sys
import unittest
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packaging"))

import build_plugin_zip as bp  # noqa: E402


class PrivatePayloadGuardTest(unittest.TestCase):
    def test_real_table_is_refused(self):
        found = bp.find_private_payload(
            [PurePosixPath("routes.json"), PurePosixPath("plugin.json")]
        )
        self.assertEqual(found, ["routes.json"])

    def test_example_table_is_allowed(self):
        self.assertEqual(bp.find_private_payload([PurePosixPath("routes.example.json")]), [])

    def test_repo_does_not_track_a_real_table(self):
        tracked = subprocess.run(
            ["git", "ls-files", "routes.json"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        if tracked.returncode != 0:
            self.skipTest("git unavailable in this environment")
        self.assertEqual(tracked.stdout.strip(), "")

    def test_collected_payload_is_runtime_only(self):
        if not (REPO / ".git").exists():
            self.skipTest("no git checkout: collect() falls back to a directory walk")
        rel = sorted(p.relative_to(REPO).as_posix() for p in bp.collect(REPO))
        self.assertIn("routes.example.json", rel)
        self.assertIn("backend/main.py", rel)
        self.assertNotIn("routes.json", rel)
        for dev_only in (
            "tests/test_middleware.py",
            ".github/workflows/ci.yml",
            "ruff.toml",
            ".gitignore",
            "packaging/build_plugin_zip.py",
        ):
            self.assertNotIn(dev_only, rel, f"{dev_only} must not ship inside a plugin")
        self.assertFalse([r for r in rel if "_cache" in r or r.endswith(".pyc")])


if __name__ == "__main__":
    unittest.main()
