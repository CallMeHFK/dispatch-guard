#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for the 2026-10-12 adversarial review (review-20261012.md).

One test per fixed finding. Stdlib-only, same conventions as
test_middleware.py (temp plugin tree outside the temp workspace, real
routes.json files, global caches saved and restored).
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

import main as dg  # noqa: E402


class FakeCall:
    def __init__(self, name, inp):
        self.name = name
        self.input = inp


async def _gen_ok():
    yield dg.ToolResponse(content=[dg.TextBlock(type="text", text="ok")])


def run(mw, name, inp, call=None):
    async def go():
        tc = call if call is not None else FakeCall(name, inp)
        return [ev async for ev in mw.on_acting(None, {"tool_call": tc}, _gen_ok)]

    return asyncio.run(go())


class ReviewRegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name
        self.saved_cache = dict(dg._CONFIG_CACHE)
        self.saved_plugin_dir = dg.PLUGIN_DIR
        self.saved_logged = dg._UNCONFIGURED_LOGGED
        self.saved_agents_cache = dict(dg._AGENTS_CACHE)
        self.saved_draft_written = dg._DRAFT_WRITTEN
        self.saved_no_owner_logged = dg._NO_OWNER_LOGGED
        # The host installs plugins outside the agent workspace; mirroring that
        # keeps PLUGIN_DIR paths absolute through _rel().
        self.plugin_tmp = tempfile.TemporaryDirectory()
        self.plugin_tree = Path(self.plugin_tmp.name)
        self.routes_path = self.plugin_tree / "routes.json"
        dg.PLUGIN_DIR = self.plugin_tree
        self._write_config({"mode": "enforce", "ext_routes": {".docx": "DocAgent"}})
        self._make_agent("DocAgent", ["文档"])
        self.mw = dg.DispatchGuardMiddleware(self.ws, "enforce")

    def tearDown(self):
        dg._CONFIG_CACHE.clear()
        dg._CONFIG_CACHE.update(self.saved_cache)
        dg.PLUGIN_DIR = self.saved_plugin_dir
        dg._UNCONFIGURED_LOGGED = self.saved_logged
        dg._AGENTS_CACHE.clear()
        dg._AGENTS_CACHE.update(self.saved_agents_cache)
        dg._DRAFT_WRITTEN = self.saved_draft_written
        dg._NO_OWNER_LOGGED = self.saved_no_owner_logged
        self.tmp.cleanup()
        self.plugin_tmp.cleanup()

    def _bust(self):
        dg._CONFIG_CACHE.clear()
        dg._CONFIG_CACHE.update({"mtime": 0.0, "data": None, "source": None})
        dg._UNCONFIGURED_LOGGED = False
        dg._AGENTS_CACHE.update({"ws": None, "agents": None, "ids": frozenset()})
        dg._DRAFT_WRITTEN = False
        dg._NO_OWNER_LOGGED = False

    def _write_config(self, cfg):
        self.routes_path.write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
        )
        self._bust()

    def _make_agent(self, agent_id, skills):
        d = Path(self.ws) / "agents" / agent_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "agent.json").write_text(
            json.dumps({"id": agent_id, "skills": skills}, ensure_ascii=False),
            encoding="utf-8",
        )

    # -- D1: the guarded agent must never rewrite the guard's own table ----

    def test_plugin_table_overwrite_is_intercepted(self):
        target = str(dg.PLUGIN_DIR / "routes.json")
        r = run(self.mw, "write_file", {"file_path": target, "content": '{"mode":"off"}'})
        self.assertEqual(r[0].state, "denied")
        self.assertIn("元完整性", r[0].content[0].text)
        # The table is untouched, and the guard is therefore still armed.
        self.assertNotEqual(
            json.loads(self.routes_path.read_text(encoding="utf-8")).get("mode"),
            "off",
        )
        r2 = run(self.mw, "write_file", {"file_path": "outputs/a.docx"})
        self.assertEqual(r2[0].state, "denied")

    def test_guard_config_denied_in_warn_mode_too(self):
        # Meta-integrity is not a mode decision: warn must deny as well.
        self._write_config({"mode": "warn", "ext_routes": {".docx": "DocAgent"}})
        target = str(dg.PLUGIN_DIR / "routes.json")
        r = run(self.mw, "write_file", {"file_path": target, "content": "{}"})
        self.assertEqual(r[0].state, "denied")

    def test_draft_table_overwrite_is_intercepted(self):
        target = str(dg.PLUGIN_DIR / "routes.draft.json")
        r = run(self.mw, "write_file", {"file_path": target, "content": "{}"})
        self.assertEqual(r[0].state, "denied")

    def test_malformed_table_is_not_mirrored(self):
        # Host layout: plugin-data is the reinstall-proof home.
        host = Path(self.plugin_tmp.name) / "host"
        dg.PLUGIN_DIR = host / "plugins" / "dispatch-guard"
        dg.PLUGIN_DIR.mkdir(parents=True)
        data_dir = host / "plugin-data" / "dispatch-guard"
        good = json.dumps({"mode": "warn", "routes": [], "ext_routes": {}})
        plugin_copy = dg.PLUGIN_DIR / "routes.json"
        plugin_copy.write_text(good, encoding="utf-8")
        self._bust()
        dg._load_config()
        self.assertEqual(
            (data_dir / "routes.json").read_text(encoding="utf-8"), good
        )
        # A malformed rewrite must not propagate over the good copy.
        plugin_copy.write_text("{not json", encoding="utf-8")
        dg._mirror_to_data_dir(plugin_copy)
        self.assertEqual(
            (data_dir / "routes.json").read_text(encoding="utf-8"), good
        )


if __name__ == "__main__":
    unittest.main()
