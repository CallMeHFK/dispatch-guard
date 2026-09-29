#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the QwenPaw plugin archive that a GitHub Release ships.

QwenPaw's two install paths disagree about archive layout and the only shape both
accept is "exactly one top-level directory holding plugin.json", so every member is
prefixed with the plugin id. The GitHub-generated source archive is installable too,
but it drops tests/ and .github/ into ~/.qwenpaw/plugins/ — this archive is the one
to point users at.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
# Dev-only payload: the runtime tree is plugin.json + backend/ + the docs README links to.
EXCLUDE = {
    ".git",
    ".github",
    ".qoder",
    "__pycache__",
    "dist",
    "packaging",
    "tests",
    ".gitignore",
    ".DS_Store",
    "ruff.toml",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "logs",
}
# The user's real dispatch table is private data: agent names, internal system
# keywords, and the shape of their whole deployment. EXCLUDE filters directory
# names only, so a root-level routes.json slips through unless checked for.
# The generated draft names this environment's real agents too, so it is
# private payload the same way. routes.example.json — the sanitized table
# shipped in the repo — is allowed.
PRIVATE_PAYLOAD = {"routes.json", "routes.draft.json"}
# Fixed timestamp: a rebuild of the same commit must byte-compare equal, so a re-run
# after a failed release job can be diffed against the asset already on GitHub.
MEMBER_DATE = (1980, 1, 1, 0, 0, 0)
MEMBER_MODE = 0o644 << 16


def collect(root: Path) -> list[Path]:
    """Files to publish. A release archive should equal the committed tree.

    Two filters stack: the tracked set decides what exists (walking the directory
    instead shipped .gitignore's job — a real ``.ruff_cache/`` landed that way),
    and EXCLUDE decides what is dev-only payload that must not sit in an
    installed plugin.
    """
    tracked = _git_tracked(root)
    candidates = tracked if tracked is not None else _walk(root)
    return [p for p in candidates if not _dev_only(p.relative_to(root))]


def _dev_only(rel: Path) -> bool:
    return (
        any(part in EXCLUDE for part in rel.parts)
        or rel.name == "__pycache__"
        or rel.suffix == ".pyc"
    )


def _walk(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        files.append(path)
    return files


def _git_tracked(root: Path) -> list[Path] | None:
    """Repo-relative files git would publish, or None outside a git checkout."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if out.returncode != 0:
        return None
    names = out.stdout.decode("utf-8", "surrogateescape").split("\0")
    paths = [root / PurePosixPath(n) for n in names if n]
    return sorted(p for p in paths if p.is_file() or p.is_symlink())


def find_private_payload(rel_paths) -> list[str]:
    """Return repo-relative paths that must never enter a published archive."""
    return sorted(str(p) for p in rel_paths if str(p) in PRIVATE_PAYLOAD)


def build(plugin_id: str, version: str) -> Path:
    files = collect(REPO)
    private = find_private_payload(path.relative_to(REPO) for path in files)
    if private:
        sys.exit(
            f"refusing to build: {', '.join(private)} is a private dispatch table "
            "(real agent ids and internal keywords). The archive ships "
            "routes.example.json only; keep your own table in the installed plugin "
            "directory, which is gitignored."
        )
    for path in files:
        if path.is_symlink():
            sys.exit(
                f"refusing to build: {path.relative_to(REPO)} is a symlink; "
                "zipfile.extractall would land it as a text file containing the "
                "target path, so the installed plugin would be broken in a way "
                "that reads like a plugin bug"
            )
    if "plugin.json" not in {p.name for p in files}:
        sys.exit("refusing to build: plugin.json not found at the repo root")

    out_dir = REPO / "dist"
    out_dir.mkdir(exist_ok=True)
    # Named after the plugin id, not the release: the version the host reads is
    # the one inside plugin.json, and `releases/latest/download/{id}.zip` stays
    # a permanent URL that never needs re-typing on upgrade.
    out = out_dir / f"{plugin_id}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            name = PurePosixPath(plugin_id, path.relative_to(REPO)).as_posix()
            info = zipfile.ZipInfo(name, date_time=MEMBER_DATE)
            info.external_attr = MEMBER_MODE
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    verify(out, plugin_id, version, len(files))
    return out


def verify(path: Path, plugin_id: str, version: str, expected_files: int) -> None:
    """Re-read the archive the way the host's two install paths would."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        roots = {n.split("/", 1)[0] for n in names}
        if roots != {plugin_id}:
            sys.exit(f"bad archive layout, top level is {sorted(roots)} not [{plugin_id}]")
        if f"{plugin_id}/plugin.json" not in names:
            sys.exit(f"bad archive layout: no {plugin_id}/plugin.json")
        manifest = json.loads(archive.read(f"{plugin_id}/plugin.json"))
        entry = manifest["entry"]["backend"]
        if f"{plugin_id}/{entry}" not in names:
            sys.exit(f"manifest entry backend {entry} is not in the archive")
        if manifest["id"] != plugin_id or manifest["version"] != version:
            sys.exit("manifest changed between build and verify")
        if any(i.create_system != 3 for i in archive.infolist()):
            sys.exit("archive members carry a non-unix origin, check the packer")
        if len(names) != expected_files:
            sys.exit(f"{len(names)} members written, expected {expected_files}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="release tag to guard against, e.g. v1.4.7")
    args = parser.parse_args()

    manifest = json.loads((REPO / "plugin.json").read_text(encoding="utf-8"))
    plugin_id, version = manifest["id"], manifest["version"]
    if args.tag and args.tag.removeprefix("v") != version:
        sys.exit(f"tag {args.tag} does not match plugin.json version {version}")

    out = build(plugin_id, version)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    payload = [
        name
        for name in zipfile.ZipFile(out).namelist()
        if not name.endswith("/")
    ]
    print(f"{out.name}  v{version}  {out.stat().st_size} bytes  sha256={digest[:16]}…")
    print(f"install URL (latest release): .../releases/latest/download/{quote(out.name)}")
    for name in payload:
        print("  ", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
