#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for dispatch-guard's middleware.

Stdlib-only: ``backend/main.py`` degrades its AgentScope/QwenPaw imports to
local stand-ins, so this suite runs anywhere (GitHub CI included) without
installing the host. Mode flips are injected through ``_CONFIG_CACHE`` from the
shipped ``routes.example.json``, so no test ever needs a real dispatch table.
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


class FakeCfg:
    def __init__(self, agent_id):
        self.id = agent_id


class FakeCtx:
    def __init__(self, workspace):
        self.workspace_dir = workspace
        self.session_id = "test"
        self.agent_id = "default"


async def _gen_ok():
    yield dg.ToolResponse(content=[dg.TextBlock(type="text", text="ok")])


def run(mw, name, inp):
    async def go():
        return [ev async for ev in mw.on_acting(None, {"tool_call": FakeCall(name, inp)}, _gen_ok)]

    return asyncio.run(go())


class DispatchGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name
        self.saved_cache = dict(dg._CONFIG_CACHE)
        self.saved_plugin_dir = dg.PLUGIN_DIR
        self.saved_logged = dg._UNCONFIGURED_LOGGED
        # The host installs plugins outside the agent workspace; mirroring that is
        # what keeps PLUGIN_DIR paths absolute through _rel() and lets the
        # absolute-path-only plugin whitelist fire.
        self.plugin_tmp = tempfile.TemporaryDirectory()
        self.plugin_tree = Path(self.plugin_tmp.name)
        self.routes_path = self.plugin_tree / "routes.json"
        # _load_config() stats PLUGIN_DIR/routes.json before touching the cache, so
        # a test that wants a configured state must write that file for real.
        dg.PLUGIN_DIR = self.plugin_tree
        self._inject_mode("enforce")
        self.mw = dg.DispatchGuardMiddleware(self.ws, "enforce")

    def tearDown(self):
        dg._CONFIG_CACHE.clear()
        dg._CONFIG_CACHE.update(self.saved_cache)
        dg.PLUGIN_DIR = self.saved_plugin_dir
        dg._UNCONFIGURED_LOGGED = self.saved_logged
        self.tmp.cleanup()
        self.plugin_tmp.cleanup()

    @staticmethod
    def _bust_config_cache():
        dg._CONFIG_CACHE.clear()
        dg._CONFIG_CACHE.update({"mtime": 0.0, "data": None})
        dg._UNCONFIGURED_LOGGED = False

    def _inject_mode(self, mode):
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = mode
        self.routes_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self._bust_config_cache()

    # -- write tools ------------------------------------------------------

    def test_deny_deliverable_write_with_route_hint(self):
        r = run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertEqual(r[0].state, "denied")
        self.assertIn("DocAgent", r[0].content[0].text)

    def test_allow_whitelist_dir(self):
        r = run(self.mw, "write_file", {"file_path": "memory/2026-09-28.md"})
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].content[0].text, "ok")

    def test_allow_baseline_file(self):
        r = run(self.mw, "edit_file", {"file_path": "AGENTS.md"})
        self.assertEqual(r[0].content[0].text, "ok")

    def test_deny_projects_generic_hint(self):
        r = run(self.mw, "write_file", {"file_path": "projects/x/reports/r.md"})
        self.assertEqual(r[0].state, "denied")

    def test_native_string_input_is_parsed(self):
        # Host-native tools deliver input as a JSON *string*; the guard must
        # parse it or the whitelist silently stops working.
        payload = json.dumps({"file_path": "memory/x.md"})
        r = run(self.mw, "write_file", payload)
        self.assertEqual(r[0].content[0].text, "ok")

    def test_ascii_keyword_word_boundary(self):
        # "CI" must not match inside "asyncio".
        self.assertEqual(self.mw._route_hit("import asyncio"), (None, None))
        agent, _why = self.mw._route_hit("setup CI pipeline")
        self.assertEqual(agent, "CodeAgent")

    def test_plugin_dir_is_whitelisted(self):
        plugin_file = str(dg.PLUGIN_DIR / "backend" / "main.py")
        r = run(self.mw, "edit_file", {"file_path": plugin_file})
        self.assertEqual(r[0].content[0].text, "ok")

    # -- shell ------------------------------------------------------------

    def test_shell_deliverable_warn_only(self):
        r = run(self.mw, "execute_shell_command", {"command": "pandoc a.md -o outputs/r.docx"})
        self.assertEqual(len(r), 1)  # not denied
        self.assertEqual(len(r[0].content), 2)
        self.assertIn("dispatch-guard warn", r[0].content[1].text)

    def test_shell_benign_passthrough(self):
        r = run(self.mw, "execute_shell_command", {"command": "ls -la && git status"})
        self.assertEqual(len(r[0].content), 1)

    # -- spawn ------------------------------------------------------------

    def test_spawn_denied(self):
        r = run(self.mw, "spawn_subagent", {"task": "blind review"})
        self.assertEqual(r[0].state, "denied")

    # -- factory / modes ---------------------------------------------------

    def test_factory_only_attaches_default_agent(self):
        self.assertIsNone(dg._factory(FakeCtx(self.ws), FakeCfg("CodeAgent")))
        self.assertIsNotNone(dg._factory(FakeCtx(self.ws), FakeCfg("default")))

    def test_mode_off_removes_factory(self):
        self._inject_mode("off")
        try:
            self.assertIsNone(dg._factory(FakeCtx(self.ws), FakeCfg("default")))
        finally:
            self._inject_mode("enforce")

    def test_warn_mode_passes_with_warning(self):
        self._inject_mode("warn")
        try:
            r = run(self.mw, "write_file", {"file_path": "outputs/x.docx"})
            self.assertEqual(len(r[0].content), 2)
            self.assertIn("warn", r[0].content[1].text)
        finally:
            self._inject_mode("enforce")

    # -- unconfigured install (no routes.json shipped in the repo) ----------

    def _fresh_plugin_tree(self, routes_content=None):
        """Rewrite the temp plugin tree; no routes.json unless content is given."""
        if routes_content is None:
            self.routes_path.unlink(missing_ok=True)
        else:
            self.routes_path.write_text(routes_content, encoding="utf-8")
        self._bust_config_cache()
        return self.plugin_tree

    def test_missing_routes_json_attaches_and_asks_to_configure(self):
        with self.assertLogs("dispatch_guard.qwenpaw", level="WARNING") as logs:
            tree = self._fresh_plugin_tree()
            mw = dg._factory(FakeCtx(self.ws), FakeCfg("default"))
        self.assertTrue(any("unconfigured" in line for line in logs.output))
        self.assertIsNotNone(mw, "unconfigured must not detach: a silent no-op "
                                 "install reads like a working one")
        self.assertEqual(mw._mode, dg.UNCONFIGURED)
        self.assertFalse((tree / "routes.json").exists())

        r = run(mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertEqual(len(r), 1, "deliverable write must pass, not deny")
        self.assertNotEqual(r[0].state, "denied")
        block = r[0].content[1].text
        self.assertIn("routes.json", block)
        self.assertIn("routes.example.json", block)

        audit = Path(self.ws) / "logs" / "dispatch_guard.jsonl"
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[-1]["action"], "needs_config")
        self.assertEqual(records[-1]["mode"], dg.UNCONFIGURED)

    def test_malformed_routes_json_still_fails_open(self):
        # Distinct from a missing file: a table we cannot read must not become a
        # blanket blocker, so the pre-0.1.2 fail-open design stands.
        self._fresh_plugin_tree("{ not json")
        with self.assertLogs("dispatch_guard.qwenpaw", level="ERROR") as logs:
            self.assertEqual(dg._load_config()["mode"], "off")
            self.assertIsNone(dg._factory(FakeCtx(self.ws), FakeCfg("default")))
        self.assertTrue(any("failing open" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
