#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Release hygiene: the version is one fact, not three copies.

plugin.json's version is what the host displays and what the release workflow
tags; the newest docs/CHANGELOG.md section is what the release page's notes are
extracted from; the README's install URL is named after the plugin id. A bump
that misses one of them ships a release whose notes describe the previous
version, or an install one-liner pointing at an asset that no longer exists —
both of which still download and still install, just not what the page claims.
"""
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packaging"))

import release_notes  # noqa: E402

MANIFEST = json.loads((REPO / "plugin.json").read_text(encoding="utf-8"))


def changelog_versions() -> list[str]:
    text = (REPO / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    return re.findall(r"^#{2,3}\s+v(\d+\.\d+\.\d+)(?:\s|$)", text, re.MULTILINE)


class ReleaseHygieneTest(unittest.TestCase):
    def test_manifest_version_is_semver(self):
        self.assertRegex(MANIFEST["version"], r"^\d+\.\d+\.\d+$")

    def test_newest_changelog_section_matches_manifest(self):
        """Sections are newest-first; the top one is the version about to ship."""
        versions = changelog_versions()
        self.assertTrue(versions, "docs/CHANGELOG.md has no vX.Y.Z sections")
        self.assertEqual(
            versions[0],
            MANIFEST["version"],
            "plugin.json version and the newest docs/CHANGELOG.md section disagree",
        )

    def test_release_notes_extractor_finds_the_newest_section(self):
        """The exact failure this guards: release_notes.py looked for `### vX.Y.Z`
        while the changelog said `## vX.Y.Z`, so every release page would have
        carried the placeholder note instead of the real one."""
        text, found = release_notes.extract(
            REPO / "docs" / "CHANGELOG.md", f"v{MANIFEST['version']}"
        )
        self.assertTrue(found)
        self.assertNotIn(release_notes.FALLBACK.format(tag=f"v{MANIFEST['version']}"), text)

    def test_readme_install_url_matches_the_built_asset(self):
        """The build names the asset after the plugin id; the README's permanent
        install URL must name the same file, or it outlives the asset it points at."""
        pid = MANIFEST["id"]
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            f"releases/latest/download/{pid}.zip",
            readme,
            f"README never installs {pid}.zip from the releases page",
        )


if __name__ == "__main__":
    unittest.main()
