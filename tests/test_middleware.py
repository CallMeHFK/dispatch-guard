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
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.saved_agents_cache = dict(dg._AGENTS_CACHE)
        self.saved_draft_written = dg._DRAFT_WRITTEN
        self.saved_no_owner_logged = dg._NO_OWNER_LOGGED
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
        dg._AGENTS_CACHE.clear()
        dg._AGENTS_CACHE.update(self.saved_agents_cache)
        dg._DRAFT_WRITTEN = self.saved_draft_written
        dg._NO_OWNER_LOGGED = self.saved_no_owner_logged
        self.tmp.cleanup()
        self.plugin_tmp.cleanup()

    @staticmethod
    def _bust_config_cache():
        dg._CONFIG_CACHE.clear()
        dg._CONFIG_CACHE.update({"mtime": 0.0, "data": None})
        dg._UNCONFIGURED_LOGGED = False
        # Discovery caches per workspace; a new fake environment must not see
        # the previous test's agents.
        dg._AGENTS_CACHE.update({"ws": None, "agents": None, "ids": frozenset()})
        dg._DRAFT_WRITTEN = False
        dg._NO_OWNER_LOGGED = False

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

    # -- basic operations must never be intercepted ------------------------

    def test_basic_writes_pass_silently_in_enforce(self):
        # Not whitelisted, but not deliverable-shaped either: notes, scripts,
        # configs, scratch and extension-less files are basic operations. They
        # pass in every mode and do not even earn a log entry.
        for target in ("todo.txt", "scripts/setup.py", "notes.md", "Makefile", "config/app.yaml"):
            r = run(self.mw, "write_file", {"file_path": target})
            self.assertEqual(r[0].content[0].text, "ok", target)
        audit = Path(self.ws) / "logs" / "dispatch_guard.jsonl"
        self.assertFalse(audit.exists(), "basic writes must not be logged")

    def test_read_operations_are_never_intercepted(self):
        # Even a deliverable path must be freely readable — the guard governs
        # who produces outputs, never who may look at them.
        for name, inp in (
            ("read_file", {"file_path": "outputs/report.docx"}),
            ("list_dir", {"path": "outputs"}),
            ("grep", {"pattern": "x", "path": "projects/"}),
        ):
            r = run(self.mw, name, inp)
            self.assertEqual(r[0].content[0].text, "ok", name)

    def test_whitelist_beats_deliverable_shape(self):
        # tmp/ is bookkeeping scratch space: a .png there is not a deliverable.
        r = run(self.mw, "write_file", {"file_path": "tmp/scratch.png"})
        self.assertEqual(r[0].content[0].text, "ok")

    def test_deliverable_exts_config_extends_defaults(self):
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        cfg["deliverable_exts"] = [".cfg"]
        self.routes_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self._bust_config_cache()
        r = run(self.mw, "write_file", {"file_path": "app.cfg"})
        self.assertEqual(r[0].state, "denied", "custom ext must be guarded")
        r = run(self.mw, "write_file", {"file_path": "outputs/x.docx"})
        self.assertEqual(r[0].state, "denied", "defaults must survive a custom ext")

    def test_unconfigured_mode_stays_silent_on_basic_writes(self):
        self._fresh_plugin_tree()
        mw = dg._factory(FakeCtx(self.ws), FakeCfg("default"))
        r = run(mw, "write_file", {"file_path": "todo.txt"})
        self.assertEqual(r[0].content[0].text, "ok")
        self.assertEqual(len(r[0].content), 1, "no setup block for basic writes")

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

    def test_shell_redirect_into_deliverable_warns(self):
        r = run(self.mw, "execute_shell_command", {"command": "echo '# hi' > outputs/README.md"})
        self.assertEqual(len(r[0].content), 2)
        self.assertIn("dispatch-guard warn", r[0].content[1].text)

    def test_shell_basic_output_is_silent(self):
        # Redirects into notes/scripts/configs are basic operations — the old
        # heuristic warned on any .md/.py redirect, which is exactly the noise
        # this plugin must not make.
        r = run(self.mw, "execute_shell_command", {
            "command": "echo hi > todo.txt && cat a.md > summary.md && python gen.py > app.cfg 2>&1"
        })
        self.assertEqual(len(r[0].content), 1)

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

    # -- environment discovery: no owner, no block --------------------------

    def _make_agents(self, spec):
        """Write a fake agent inventory into the workspace, host-style."""
        for aid, skills in spec.items():
            d = Path(self.ws) / "agents" / aid
            d.mkdir(parents=True, exist_ok=True)
            (d / "agent.json").write_text(
                json.dumps({"id": aid, "skills": skills, "description": ""}, ensure_ascii=False),
                encoding="utf-8",
            )

    def _audit_records(self):
        path = Path(self.ws) / "logs" / "dispatch_guard.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_discovery_reads_workspace_agents(self):
        self._make_agents({"DocAgent": ["文档", "报告"], "CodeAgent": ["代码"]})
        ids = {a["id"] for a in dg._discover_agents(self.ws)}
        self.assertEqual(ids, {"DocAgent", "CodeAgent"})

    def test_stale_route_to_absent_agent_stands_down(self):
        # The example table routes .docx -> DocAgent, but this environment has
        # no DocAgent. Denying would strand the deliverable with nowhere to
        # dispatch to, so the write passes and the audit says no_owner.
        self._make_agents({"CodeAgent": ["代码"]})
        r = run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertNotEqual(r[0].state, "denied")
        self.assertEqual(len(r[0].content), 1, "no warning block: there is no agent to name")
        self.assertEqual(self._audit_records()[-1]["action"], "no_owner")

    def test_live_route_still_denies(self):
        self._make_agents({"DocAgent": ["文档"]})
        r = run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertEqual(r[0].state, "denied")
        self.assertIn("DocAgent", r[0].content[0].text)

    def test_empty_discovery_trusts_the_table(self):
        # No agent manifests anywhere we can read: discovery cannot judge the
        # deployment, so the operator's table applies verbatim (pre-discovery
        # behaviour) instead of silently disarming the guard.
        r = run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertEqual(r[0].state, "denied")

    def test_specialists_exist_but_none_owns_the_class(self):
        # The motivating scenario: a real multi-agent env with no doc/design
        # specialist. Poster writes must pass even in enforce mode.
        self._make_agents({"CodeAgent": ["代码"]})
        r = run(self.mw, "write_file", {"file_path": "outputs/poster.svg"})
        self.assertNotEqual(r[0].state, "denied")
        self.assertEqual(len(r[0].content), 1)
        self.assertEqual(self._audit_records()[-1]["action"], "no_owner")

    def test_shell_no_owner_is_silent(self):
        self._make_agents({"CodeAgent": ["代码"]})
        r = run(self.mw, "execute_shell_command", {"command": "echo hi > outputs/banner.svg"})
        self.assertEqual(len(r[0].content), 1)

    # -- draft generation from the environment ------------------------------

    def test_unconfigured_generates_draft_from_environment(self):
        self._fresh_plugin_tree()
        self._make_agents({"DocAgent": ["文档", "报告"], "CodeAgent": ["代码"]})
        mw = dg._factory(FakeCtx(self.ws), FakeCfg("default"))
        self.assertIsNotNone(mw)

        draft = self.plugin_tree / "routes.draft.json"
        self.assertTrue(draft.exists(), "attach must draft a table when agents are visible")
        cfg = json.loads(draft.read_text(encoding="utf-8"))
        self.assertEqual(cfg["mode"], "warn", "a draft never activates enforce by itself")
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")
        self.assertNotIn(".svg", cfg["ext_routes"], "no design agent -> media group stays unrouted")
        self.assertFalse((self.plugin_tree / "routes.json").exists())

        # Unconfigured still never denies; the setup block now names the draft.
        r = run(mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertNotEqual(r[0].state, "denied")
        self.assertIn("routes.draft.json", r[0].content[1].text)

    def test_draft_confirmed_then_enforces_end_to_end(self):
        # Full onboarding: env -> draft -> review/rename -> warn -> enforce,
        # with basics and ownerless classes passing throughout.
        self._fresh_plugin_tree()
        self._make_agents({"DocAgent": ["文档"]})
        dg._factory(FakeCtx(self.ws), FakeCfg("default"))
        draft = self.plugin_tree / "routes.draft.json"
        self.assertTrue(draft.exists())

        # Operator reviews, keeps warn, renames to activate.
        cfg = json.loads(draft.read_text(encoding="utf-8"))
        self.assertEqual(cfg["mode"], "warn")
        self.routes_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self._bust_config_cache()
        r = run(self.mw, "write_file", {"file_path": "outputs/x.docx"})
        self.assertEqual(len(r[0].content), 2)
        self.assertIn("DocAgent", r[0].content[1].text)

        # Confidence earned: flip to enforce.
        cfg["mode"] = "enforce"
        self.routes_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self._bust_config_cache()
        self.assertEqual(run(self.mw, "write_file", {"file_path": "outputs/x.docx"})[0].state, "denied")
        # And the two stand-down guarantees hold under enforce:
        self.assertEqual(run(self.mw, "write_file", {"file_path": "todo.txt"})[0].content[0].text, "ok")
        r = run(self.mw, "write_file", {"file_path": "outputs/poster.svg"})
        self.assertNotEqual(r[0].state, "denied")

    def test_unconfigured_without_specialists_writes_no_draft(self):
        self._fresh_plugin_tree()
        mw = dg._factory(FakeCtx(self.ws), FakeCfg("default"))
        self.assertFalse((self.plugin_tree / "routes.draft.json").exists())
        self.assertIsNotNone(mw)
        r = run(mw, "write_file", {"file_path": "outputs/x.docx"})
        self.assertIn("routes.example.json", r[0].content[1].text)

    # -- real QwenPaw host layout -------------------------------------------
    # The host keeps one workspace per agent (workspaces/<id>/agent.json) and
    # the authoritative enabled flags in config.json agents.profiles. The
    # orchestrator itself and explicitly disabled agents are never targets.

    def _make_host_tree(self, agents_spec, profiles):
        """<tmp>/host/plugins/dispatch-guard + <tmp>/host/{workspaces,config.json}.

        Redirects dg.PLUGIN_DIR into host/plugins/ so the host-probing gate
        (_host_tree: parent.name == "plugins") actually fires, mirroring
        ~/.qwenpaw/plugins/dispatch-guard. The extra "host" level is what
        makes parent.parent resolve to the fake QwenPaw home.
        """
        host = Path(self.plugin_tmp.name) / "host"
        dg.PLUGIN_DIR = host / "plugins" / "dispatch-guard"
        dg.PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        for aid, desc in agents_spec.items():
            d = host / "workspaces" / aid
            d.mkdir(parents=True, exist_ok=True)
            (d / "agent.json").write_text(
                json.dumps({"id": aid, "description": desc}, ensure_ascii=False),
                encoding="utf-8",
            )
        (host / "config.json").write_text(
            json.dumps({"agents": {"profiles": profiles}}, ensure_ascii=False),
            encoding="utf-8",
        )
        return host

    def test_host_workspace_layout_is_discovered(self):
        host = self._make_host_tree(
            {"DocAgent": "合同标书", "DesignAgent": "图形", "default": "编排者"},
            {"DocAgent": {"enabled": True}, "DesignAgent": {"enabled": True}, "Qoder": {"enabled": False}},
        )
        (host / "workspaces" / "Qoder").mkdir()
        (host / "workspaces" / "Qoder" / "agent.json").write_text(
            json.dumps({"id": "Qoder", "description": "编码"}, ensure_ascii=False),
            encoding="utf-8",
        )
        self._bust_config_cache()
        ids = {a["id"] for a in dg._discover_agents(self.ws)}
        self.assertIn("DocAgent", ids)
        self.assertIn("DesignAgent", ids)
        self.assertNotIn("default", ids, "the orchestrator is never a dispatch target")
        self.assertNotIn("Qoder", ids, "explicitly disabled agents are not dispatch targets")

    def test_unreadable_host_config_excludes_nothing(self):
        # Fail open: a broken config.json must not shrink the inventory and
        # thereby invent no_owner verdicts for live agents.
        host = Path(self.plugin_tmp.name) / "host"
        dg.PLUGIN_DIR = host / "plugins" / "dispatch-guard"
        dg.PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        (host / "workspaces" / "DocAgent").mkdir(parents=True)
        (host / "workspaces" / "DocAgent" / "agent.json").write_text(
            json.dumps({"id": "DocAgent"}, ensure_ascii=False), encoding="utf-8"
        )
        (host / "config.json").write_text("{ not json", encoding="utf-8")
        self._bust_config_cache()
        self.assertEqual({a["id"] for a in dg._discover_agents(self.ws)}, {"DocAgent"})

    # -- inventory hot-reload & absolute paths -------------------------------

    def test_inventory_refreshes_when_a_manifest_changes(self):
        # The inventory is mtime-stamped the same way routes.json is: an agent
        # added or edited mid-process must be visible on the next guarded call
        # without a host restart.
        self._make_agents({"DocAgent": ["文档"]})
        self.assertEqual({a["id"] for a in dg._discover_agents(self.ws)}, {"DocAgent"})
        self._make_agents({"CodeAgent": ["代码"]})
        self.assertEqual(
            {a["id"] for a in dg._discover_agents(self.ws)},
            {"DocAgent", "CodeAgent"},
        )

    def test_enabled_flag_refresh_excludes_parked_agent(self):
        host = self._make_host_tree({"DocAgent": "文档"}, {"DocAgent": {"enabled": True}})
        self._bust_config_cache()
        self.assertIn("DocAgent", {a["id"] for a in dg._discover_agents(self.ws)})
        (host / "config.json").write_text(
            json.dumps({"agents": {"profiles": {"DocAgent": {"enabled": False}}}}),
            encoding="utf-8",
        )
        self.assertNotIn("DocAgent", {a["id"] for a in dg._discover_agents(self.ws)})

    def test_absolute_path_into_deliverable_dir_is_guarded(self):
        # _rel leaves absolute targets untouched, so prefix matching against
        # workspace-relative dirs never fired for them; a segment match does.
        r = run(self.mw, "write_file", {"file_path": "/somewhere/else/outputs/quarterly-report"})
        self.assertEqual(r[0].state, "denied")

    def test_deep_relative_projects_dir_stays_a_basic_op(self):
        # Segment matching is for absolute paths only: a nested projects/ in a
        # source tree is not the deployment's deliverable directory.
        r = run(self.mw, "write_file", {"file_path": "src/myapp/projects/notes.txt"})
        self.assertEqual(r[0].content[0].text, "ok")

    # -- audit trail rotation -------------------------------------------------

    def test_audit_log_rotates_past_max_bytes(self):
        saved = dg.AUDIT_MAX_BYTES
        dg.AUDIT_MAX_BYTES = 400
        try:
            for _ in range(6):
                run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        finally:
            dg.AUDIT_MAX_BYTES = saved
        log = Path(self.ws) / "logs" / "dispatch_guard.jsonl"
        backup = Path(self.ws) / "logs" / "dispatch_guard.jsonl.1"
        self.assertTrue(backup.exists(), "rotation keeps one backup generation")
        self.assertTrue(log.exists())
        self.assertGreaterEqual(backup.stat().st_size, 400)
        self.assertLess(
            log.stat().st_size,
            400 + 400,  # threshold + at most one record appended after it
            "after rotation the fresh log starts small",
        )

    # -- draft follows the orchestrator's own dispatch policy ----------------

    def _write_default_manifest(self, description):
        (Path(self.ws) / "agent.json").write_text(
            json.dumps({"id": "default", "description": description}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_draft_parses_dispatch_policy_from_default_manifest(self):
        # A deployment states routing in default's description:
        # "文档/报告/Office文档派 DocAgent，图形/图像/视频/PPT派 DesignAgent，…"
        self._make_agents({"DocAgent": ["文档"], "DesignAgent": ["图形"], "CodeAgent": ["代码"]})
        self._write_default_manifest(
            "编排者：分解任务、派发与验收汇总；文档/报告/Office文档派 DocAgent，"
            "图形/图像/视频/PPT派 DesignAgent，代码/仿真/CI派 CodeAgent；"
            "第三方宿主仅显式点名时派；自身不直接执行专业工作"
        )
        cfg = dg._draft_route_table(self.ws)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["mode"], "warn")
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")
        self.assertEqual(cfg["ext_routes"][".png"], "DesignAgent")
        self.assertNotIn(".dxf", cfg["ext_routes"], "no hardware claim in the policy")
        self.assertNotIn(".py", cfg["ext_routes"], "code stays a basic operation")
        agents = {route["agent"] for route in cfg["routes"]}
        self.assertEqual(agents, {"DocAgent", "DesignAgent"})

    def test_policy_claim_to_absent_agent_is_dropped(self):
        # "Office文档派 DocAgent" with no DocAgent installed must not route
        # documents anywhere — a policy pointing at a ghost is not a route.
        # '归档打包派 CodeAgent' matches no archive preset term either, so the
        # whole draft is vacuous.
        self._make_agents({"CodeAgent": ["代码"]})
        self._write_default_manifest("编排者；Office文档派 DocAgent，归档打包派 CodeAgent")
        self.assertIsNone(dg._draft_route_table(self.ws))

    def test_no_policy_falls_back_to_manifest_scan(self):
        # Without a dispatch policy in default's description, the draft still
        # guesses from the specialists' own ids/skills/descriptions.
        self._make_agents({"DocAgent": ["文档", "报告"]})
        cfg = dg._draft_route_table(self.ws)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")

    # -- policy lint (static checks at load time) ---------------------------

    def _configure(self, cfg):
        self.routes_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self._bust_config_cache()
        # Lint runs at config load; force the load inside the caller's
        # assertLogs context so the warnings are captured.
        dg._load_config()

    def test_lint_flags_contradictory_keyword(self):
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        cfg["routes"] = [
            {"match": ["报告"], "agent": "DocAgent"},
            {"match": ["报告"], "agent": "CodeAgent"},
        ]
        with self.assertLogs("dispatch_guard.qwenpaw", level="WARNING") as logs:
            self._configure(cfg)
        self.assertTrue(any("both DocAgent" in line and "CodeAgent" in line for line in logs.output))

    def test_lint_flags_dead_ext_route(self):
        # .py is deliberately a basic operation: an ext_routes entry for it can
        # never fire, which reads like routing that does not exist.
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        cfg["ext_routes"] = {".py": "CodeAgent", ".docx": "DocAgent"}
        with self.assertLogs("dispatch_guard.qwenpaw", level="WARNING") as logs:
            self._configure(cfg)
        self.assertTrue(any(".py" in line and "never fire" in line for line in logs.output))
        # the live entry stays silent
        self.assertFalse(any(".docx" in line and "never fire" in line for line in logs.output))

    def test_lint_flags_unknown_mode(self):
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforc"
        with self.assertLogs("dispatch_guard.qwenpaw", level="WARNING") as logs:
            self._configure(cfg)
        self.assertTrue(any("'enforc'" in line for line in logs.output))

    def test_lint_silent_on_clean_table(self):
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        logger = logging.getLogger("dispatch_guard.qwenpaw")
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture()
        logger.addHandler(handler)
        try:
            self._configure(cfg)
        finally:
            logger.removeHandler(handler)
        self.assertFalse(
            [r for r in records if "lint" in r.getMessage()],
            "the shipped example table must lint clean",
        )

    # -- coverage extensions (write_tools / shell_enforce) -------------------

    def test_write_tools_config_extends_coverage(self):
        # Host-specific file-producing tools (code runners, downloaders) are
        # operator-known; write_tools lets routes.json cover them.
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        cfg["write_tools"] = ["save_artifact"]
        self._configure(cfg)
        r = run(self.mw, "save_artifact", {"file_path": "outputs/report.docx"})
        self.assertEqual(r[0].state, "denied")
        # basic writes through the same tool stay free
        r = run(self.mw, "save_artifact", {"file_path": "todo.txt"})
        self.assertEqual(r[0].content[0].text, "ok")

    def test_shell_enforce_default_off_keeps_warn(self):
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        self._configure(cfg)
        r = run(self.mw, "execute_shell_command", {"command": "echo hi > outputs/r.docx"})
        self.assertEqual(len(r[0].content), 2, "default: warn-only")
        self.assertNotEqual(r[0].state, "denied")

    def test_shell_enforce_opt_in_denies_deliverable_output(self):
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        cfg["shell_enforce"] = True
        self._configure(cfg)
        r = run(self.mw, "execute_shell_command", {"command": "echo '# report' > outputs/r.docx"})
        self.assertEqual(r[0].state, "denied")
        # basic outputs remain silent even with shell_enforce on
        r = run(self.mw, "execute_shell_command", {"command": "echo hi > todo.txt"})
        self.assertEqual(len(r[0].content), 1)

    def test_shell_enforce_still_respects_no_owner(self):
        self._make_agents({"CodeAgent": ["代码"]})
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        cfg["shell_enforce"] = True
        cfg["deliverable_exts"] = [".7z"]
        cfg["ext_routes"] = {".7z": "OpsAgent"}
        self._configure(cfg)
        r = run(self.mw, "execute_shell_command", {"command": "tar czf - data/ > outputs/bundle.7z"})
        self.assertNotEqual(r[0].state, "denied", "no owner, no block — even via shell")

    # -- coverage tiers: explicit write targets vs quoted mentions -----------

    def test_shell_cp_destination_is_an_explicit_target(self):
        # A copy destination is where the bytes land: warn-only by default,
        # denied with shell_enforce — closing the bypass where an agent under
        # enforce moves a generated deliverable instead of redirecting it.
        cmd = {"command": "cp /tmp/gen.docx outputs/report.docx"}
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        self._configure(cfg)
        r = run(self.mw, "execute_shell_command", cmd)
        self.assertEqual(len(r[0].content), 2, "default: warn only")
        self.assertNotEqual(r[0].state, "denied")
        cfg["shell_enforce"] = True
        self._configure(cfg)
        r = run(self.mw, "execute_shell_command", cmd)
        self.assertEqual(r[0].state, "denied")

    def test_quoted_mention_in_shell_warns_but_never_denies(self):
        # A path inside a quoted string is a mention (open('outputs/x.docx'…)):
        # enough to flag, never enough to block — a grep for a filename is not
        # a write, and mention-tier denials would wedge legitimate work.
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        cfg["shell_enforce"] = True
        self._configure(cfg)
        r = run(self.mw, "execute_shell_command", {
            "command": "python -c \"open('outputs/x.docx','w').write('hi')\""
        })
        self.assertEqual(len(r), 1)
        self.assertNotEqual(r[0].state, "denied")
        self.assertEqual(len(r[0].content), 2)
        self.assertIn("提到", r[0].content[1].text)

    def test_pathless_write_tools_are_scanned_not_skipped(self):
        # Declaring a code-runner-style tool in write_tools without scanning
        # its input body would be a fake coverage: the path lives in the code.
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        cfg["write_tools"] = ["run_python"]
        self._configure(cfg)
        # explicit output shape inside the argument -> full write-tool flow
        r = run(self.mw, "run_python", {"script": "subprocess: pandoc a.md -o outputs/r.docx"})
        self.assertEqual(r[0].state, "denied")
        # quoted mention -> warn only
        r = run(self.mw, "run_python", {"code": "open('outputs/x.docx','w')"})
        self.assertNotEqual(r[0].state, "denied")
        self.assertEqual(len(r[0].content), 2)
        # code that mentions nothing deliverable stays silent
        r = run(self.mw, "run_python", {"code": "print('42')"})
        self.assertEqual(len(r[0].content), 1)

    def test_bare_string_path_input_still_counts_as_path(self):
        # Host variants of write tools may pass the path as a bare string;
        # that is an explicit target, not a mention.
        r = run(self.mw, "write_file", "outputs/report.docx")
        self.assertEqual(r[0].state, "denied")

    # -- path traversal must not dress a destination as something else -------

    def _host_plugin_tree(self, cfg_overrides=None):
        """PLUGIN_DIR under <tmp>/host/plugins/: workspace escapes resolve far
        away from PLUGIN_DIR.parent, so the traversal cases below judge the
        real destination instead of colliding with the plugin-maintenance
        whitelist (both plain tmpdirs share /tmp as a parent)."""
        host = Path(self.plugin_tmp.name) / "host"
        dg.PLUGIN_DIR = host / "plugins" / "dispatch-guard"
        dg.PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        if cfg_overrides:
            cfg.update(cfg_overrides)
        (dg.PLUGIN_DIR / "routes.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
        )
        self._bust_config_cache()

    def test_traversal_escape_cannot_dress_as_whitelist(self):
        # notes/../../outputs/escape.docx lands OUTSIDE the workspace; the raw
        # spelling's "notes/" prefix must not whitelist the real destination.
        self._host_plugin_tree()
        r = run(self.mw, "write_file", {"file_path": "notes/../../outputs/escape.docx"})
        self.assertEqual(r[0].state, "denied")
        r = run(self.mw, "write_file", {"file_path": "tmp/../../../outputs/escape2.docx"})
        self.assertEqual(r[0].state, "denied")

    def test_traversal_escape_through_shell_is_seen(self):
        self._host_plugin_tree({"shell_enforce": True})
        r = run(
            self.mw,
            "execute_shell_command",
            {"command": "echo x > notes/../../../outputs/e.docx"},
        )
        self.assertEqual(r[0].state, "denied")

    def test_traversal_staying_inside_ws_is_normalized(self):
        # notes/../outputs/inws.docx is the same file as outputs/inws.docx.
        self._host_plugin_tree()
        r = run(self.mw, "write_file", {"file_path": "notes/../outputs/inws.docx"})
        self.assertEqual(r[0].state, "denied")

    def test_traversal_escape_to_notes_is_not_a_deliverable(self):
        # The reverse dressing: outputs/../../notes/x.md really lands in a
        # notes directory outside the workspace — a basic operation that must
        # not be denied nor audited as a deliverable.
        self._host_plugin_tree()
        r = run(self.mw, "write_file", {"file_path": "outputs/../../notes/x.md"})
        self.assertEqual(r[0].content[0].text, "ok")
        audit = Path(self.ws) / "logs" / "dispatch_guard.jsonl"
        self.assertFalse(audit.exists())

    def test_plugin_whitelist_survives_the_rel_rework(self):
        self._host_plugin_tree()
        r = run(self.mw, "write_file", {"file_path": str(dg.PLUGIN_DIR / "backend" / "main.py")})
        self.assertEqual(r[0].content[0].text, "ok")

    # -- malformed host config / routes.json must degrade, never crash -------

    def test_host_config_wrong_shapes_exclude_nothing(self):
        # config.json whose agents/profiles are the wrong JSON type used to
        # raise AttributeError out of discovery and crash every guarded call.
        host = Path(self.plugin_tmp.name) / "host"
        dg.PLUGIN_DIR = host / "plugins" / "dispatch-guard"
        dg.PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        (host / "workspaces" / "DocAgent").mkdir(parents=True)
        (host / "workspaces" / "DocAgent" / "agent.json").write_text(
            json.dumps({"id": "DocAgent"}, ensure_ascii=False), encoding="utf-8"
        )
        for bad in (
            '{"agents": null}',
            '{"agents": []}',
            '{"agents": {"profiles": []}}',
            "{ not json",
        ):
            (host / "config.json").write_text(bad, encoding="utf-8")
            self._bust_config_cache()
            self.assertEqual(
                {a["id"] for a in dg._discover_agents(self.ws)},
                {"DocAgent"},
                bad,
            )

    def test_wrong_typed_ext_routes_degrades_not_crashes(self):
        # The first call failed open and every later call crashed with
        # AttributeError: the poisoned cache outlived the load-time guard.
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        cfg["ext_routes"] = ["oops"]
        self.routes_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self._bust_config_cache()
        for _ in range(3):
            r = run(self.mw, "write_file", {"file_path": "outputs/a.docx"})
            self.assertEqual(r[0].state, "denied")

    def test_wrong_typed_routes_and_null_ext_routes_degrade(self):
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        cfg["routes"] = "abc"
        cfg["ext_routes"] = None
        self.routes_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self._bust_config_cache()
        for _ in range(2):
            r = run(self.mw, "write_file", {"file_path": "outputs/a.docx"})
            self.assertEqual(r[0].state, "denied")

    def test_route_rule_with_non_array_match_keeps_other_rules(self):
        cfg = json.loads((REPO / "routes.example.json").read_text(encoding="utf-8"))
        cfg["mode"] = "enforce"
        cfg["routes"] = [
            {"match": "报告", "agent": "DocAgent"},
            {"match": ["CI"], "agent": "CodeAgent"},
        ]
        self.routes_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self._bust_config_cache()
        self.assertEqual(self.mw._route_hit("outputs/CI-thing.md")[0], "CodeAgent")
        self.assertEqual(self.mw._route_hit("outputs/x.md"), (None, None))

    def test_non_object_routes_json_fails_open(self):
        self._fresh_plugin_tree("[1, 2]")
        self.assertEqual(dg._load_config()["mode"], "off")

    # -- shell coverage: ffmpeg positional output, converter outdirs, globs --

    def test_shell_ffmpeg_positional_output_is_seen(self):
        # ffmpeg has no -o flag; its output is the trailing positional token.
        r = run(self.mw, "execute_shell_command", {"command": "ffmpeg -i clip.mov clip.mp4"})
        self.assertEqual(len(r[0].content), 2)
        self.assertIn("dispatch-guard warn", r[0].content[1].text)

    def test_shell_ffmpeg_probe_is_silent(self):
        # "ffmpeg -i clip.mov" reads; the -i input must not pose as an output.
        r = run(self.mw, "execute_shell_command", {"command": "ffmpeg -i clip.mov"})
        self.assertEqual(len(r[0].content), 1)

    def test_shell_ffmpeg_multi_input_last_token_is_output(self):
        r = run(self.mw, "execute_shell_command", {"command": "ffmpeg -y -i a.mov -i b.mov out.mp4"})
        self.assertEqual(len(r[0].content), 2)

    def test_shell_converter_outdir_is_seen(self):
        r = run(self.mw, "execute_shell_command", {
            "command": "soffice --headless --convert-to docx --outdir outputs report.odt"
        })
        self.assertEqual(len(r[0].content), 2)

    def test_deliverable_directory_itself_is_a_target(self):
        r = run(self.mw, "execute_shell_command", {"command": "soffice --outdir=outputs/ x.odt"})
        self.assertEqual(len(r[0].content), 2)

    def test_shell_glob_mention_is_silent(self):
        # '*.docx' names a class of files, not a deliverable target.
        r = run(self.mw, "execute_shell_command", {"command": "find . -name '*.docx'"})
        self.assertEqual(len(r[0].content), 1)

    # -- draft policy parsing: every claim, across sentences and phrasings ---

    def test_policy_claims_across_sentences_and_pai_gei(self):
        # "。" is a sentence boundary the old parser swallowed, and "派给 X"
        # is a phrasing it never matched — both lost whole routing claims.
        self._make_agents({"DocAgent": ["文档"], "DesignAgent": ["图形"]})
        self._write_default_manifest("文档派 DocAgent。图形派给 DesignAgent")
        cfg = dg._draft_route_table(self.ws)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")
        self.assertEqual(cfg["ext_routes"][".png"], "DesignAgent")

    # -- discovery resilience -------------------------------------------------

    def test_failed_discovery_is_not_cached(self):
        # A transient read error used to cache a PARTIAL inventory keyed to
        # the unchanged stamp — never retried, inventing no_owner verdicts.
        self._make_agents({"DocAgent": ["文档"]})
        bad_root = mock.MagicMock()
        bad_root.is_dir.return_value = True
        bad_root.glob.side_effect = OSError(5, "Injected I/O error")
        with mock.patch.object(
            dg, "_discovery_roots", return_value=[Path(self.ws) / "agents", bad_root]
        ):
            agents = dg._discover_agents(self.ws)
        self.assertEqual([a["id"] for a in agents], ["DocAgent"])
        self.assertIsNone(dg._AGENTS_CACHE["agents"], "partial inventory must not be cached")


if __name__ == "__main__":
    unittest.main()
