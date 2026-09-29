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
            {"IPP": "验收报告", "Designer": "图形", "default": "编排者"},
            {"IPP": {"enabled": True}, "Designer": {"enabled": True}, "Qoder": {"enabled": False}},
        )
        (host / "workspaces" / "Qoder").mkdir()
        (host / "workspaces" / "Qoder" / "agent.json").write_text(
            json.dumps({"id": "Qoder", "description": "编码"}, ensure_ascii=False),
            encoding="utf-8",
        )
        self._bust_config_cache()
        ids = {a["id"] for a in dg._discover_agents(self.ws)}
        self.assertIn("IPP", ids)
        self.assertIn("Designer", ids)
        self.assertNotIn("default", ids, "the orchestrator is never a dispatch target")
        self.assertNotIn("Qoder", ids, "explicitly disabled agents are not dispatch targets")

    def test_unreadable_host_config_excludes_nothing(self):
        # Fail open: a broken config.json must not shrink the inventory and
        # thereby invent no_owner verdicts for live agents.
        host = Path(self.plugin_tmp.name) / "host"
        dg.PLUGIN_DIR = host / "plugins" / "dispatch-guard"
        dg.PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        (host / "workspaces" / "IPP").mkdir(parents=True)
        (host / "workspaces" / "IPP" / "agent.json").write_text(
            json.dumps({"id": "IPP"}, ensure_ascii=False), encoding="utf-8"
        )
        (host / "config.json").write_text("{ not json", encoding="utf-8")
        self._bust_config_cache()
        self.assertEqual({a["id"] for a in dg._discover_agents(self.ws)}, {"IPP"})

    # -- draft follows the orchestrator's own dispatch policy ----------------

    def _write_default_manifest(self, description):
        (Path(self.ws) / "agent.json").write_text(
            json.dumps({"id": "default", "description": description}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_draft_parses_dispatch_policy_from_default_manifest(self):
        # The real deployment states routing in default's description:
        # "专利/规格书/Office文档派 IPP，图形/图像/视频/PPT派 Designer，…"
        self._make_agents({"IPP": ["专利"], "Designer": ["图形"], "CodeAgent": ["代码"]})
        self._write_default_manifest(
            "编排者：分解任务、派发与验收汇总；专利/规格书/Office文档派 IPP，"
            "图形/图像/视频/PPT派 Designer，代码/仿真/CI派 CodeAgent；"
            "Qoder 仅 Han 点名时派；自身不直接执行专业工作"
        )
        cfg = dg._draft_route_table(self.ws)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["mode"], "warn")
        self.assertEqual(cfg["ext_routes"][".docx"], "IPP")
        self.assertEqual(cfg["ext_routes"][".png"], "Designer")
        self.assertNotIn(".dxf", cfg["ext_routes"], "no hardware claim in the policy")
        self.assertNotIn(".py", cfg["ext_routes"], "code stays a basic operation")
        agents = {route["agent"] for route in cfg["routes"]}
        self.assertEqual(agents, {"IPP", "Designer"})

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


if __name__ == "__main__":
    unittest.main()
