#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 tests: jev semantic arbitration of the blind-spot no_owner pass.

Hook point (docs/JEV_INTEGRATION.md §3 P1): ``_guard_target``'s
``not blockable`` branch — a deliverable whose keyword route named no live
owner in a readable, non-empty inventory, which used to pass silently. The
local judge is asked which specialist owns it and only a confident pick is
handed back to the existing deny/warn path.

Rubric coverage (§5 / task rubric 1-6, 9):
1. gating matrix — p=0.90 deny(enforce)/warn(warn-mode), p=0.70 warn,
   p=0.50 no_owner, choice basic_op/none no_owner;
2. latency cap — a judge slower than the cap fails open to no_owner with an
   audited fallback=timeout and never wedges the agent;
3. D7 / L1 — a genuine route hit pays ZERO judge requests (the meta-table
   write's zero-judge guarantee lives in test_jev_draft.py);
4. third branch — an unreadable inventory still escalates to the user with
   ZERO judge requests (never arbitrated);
5. regression — jev disabled never dials out (zero judge requests) and the
   no_owner record is byte-identical to the pre-jev plugin;
6. audit — all three P1 terminal states carry the jev subobject's five fields;
9. deny/warn reuse the routed-owner text verbatim and name the judge's owner.

Stdlib-only, like the plugin.
"""
import asyncio  # noqa: F401  (used via test_middleware.run)
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "tests"))

import main as dg  # noqa: E402
import test_middleware as tm  # noqa: E402
from test_jev_client import FakeSystemOne  # noqa: E402


def _choice(name, p):
    """A FakeSystemOne responder that always answers q0 with (name, p)."""
    return lambda body: {"answers": {"q0": {"type": "choice", "choice": name,
                                            "probabilities": {name: p}}}}


class JevBlindspotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        (self.ws / "logs").mkdir(parents=True)
        self.plugin_tmp = tempfile.TemporaryDirectory()
        self.plugin_tree = Path(self.plugin_tmp.name)
        self.saved = {
            "config": dict(dg._CONFIG_CACHE),
            "plugin_dir": dg.PLUGIN_DIR,
            "agents": dict(dg._AGENTS_CACHE),
            "no_owner": dg._NO_OWNER_LOGGED,
            "unconfigured": dg._UNCONFIGURED_LOGGED,
            "off": dg._OFF_LOGGED,
            "draft": dg._DRAFT_WRITTEN,
            "env": os.environ.get("JEV_KERNEL_CONFIG"),
        }
        dg.PLUGIN_DIR = self.plugin_tree
        self.kernel_cfg = Path(self.tmp.name) / "jev.json"
        os.environ["JEV_KERNEL_CONFIG"] = str(self.kernel_cfg)
        self._make_agents({"DocAgent": ["文档"], "CodeAgent": ["代码"]})
        self.mw = dg.DispatchGuardMiddleware(str(self.ws), "enforce")

    def tearDown(self):
        dg._CONFIG_CACHE.clear()
        dg._CONFIG_CACHE.update(self.saved["config"])
        dg._AGENTS_CACHE.clear()
        dg._AGENTS_CACHE.update(self.saved["agents"])
        dg.PLUGIN_DIR = self.saved["plugin_dir"]
        dg._NO_OWNER_LOGGED = self.saved["no_owner"]
        dg._UNCONFIGURED_LOGGED = self.saved["unconfigured"]
        dg._OFF_LOGGED = self.saved["off"]
        dg._DRAFT_WRITTEN = self.saved["draft"]
        if self.saved["env"] is None:
            os.environ.pop("JEV_KERNEL_CONFIG", None)
        else:
            os.environ["JEV_KERNEL_CONFIG"] = self.saved["env"]
        self.tmp.cleanup()
        self.plugin_tmp.cleanup()

    # -- fixtures ----------------------------------------------------------

    def _make_agents(self, by_id):
        root = self.ws / "agents"
        for aid, skills in by_id.items():
            d = root / aid
            d.mkdir(parents=True, exist_ok=True)
            (d / "agent.json").write_text(
                json.dumps({"id": aid, "skills": skills,
                            "description": f"{aid} specialist"}),
                encoding="utf-8",
            )

    def _bust(self):
        dg._CONFIG_CACHE.update({"mtime": 0.0, "data": None, "source": None})
        dg._AGENTS_CACHE.update(
            {"ws": None, "agents": None, "ids": frozenset(), "stamp": None}
        )
        dg._NO_OWNER_LOGGED = False

    def _write_config(self, jev_block, mode="enforce", ext_routes=None):
        cfg = {
            "mode": mode,
            "deliverable_dirs": ["outputs", "projects"],
            "deliverable_exts": [],
            "shell_enforce": False,
            "write_tools": [],
            "routes": [],
            "ext_routes": ext_routes or {},
        }
        if jev_block is not None:
            cfg["jev"] = jev_block
        (self.plugin_tree / "routes.json").write_text(
            json.dumps(cfg), encoding="utf-8"
        )

    def _write_kernel(self, judges, **over):
        cfg = {"judges": judges}
        cfg.update(over)
        self.kernel_cfg.write_text(json.dumps(cfg), encoding="utf-8")

    @staticmethod
    def _lux(url):
        return {"type": "systemone", "baseUrl": url,
                "model": "StartLux-Decision-2B-Q8_0", "timeoutMs": 3000}

    @staticmethod
    def _jev_block(**over):
        block = {"enabled": True, "judges": "inherit:jev-kernel", "tiers": ["lux"]}
        block.update(over)
        return block

    def _audit(self):
        path = self.ws / "logs" / "dispatch_guard.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines()]

    # -- rubric 1 + 6 + 9: gating matrix, audit, reused text ----------------

    def test_confident_pick_denies_and_names_owner_enforce(self):
        with FakeSystemOne(_choice("CodeAgent", 0.90)) as lux:
            self._write_kernel({"lux": self._lux(lux.url)})
            self._write_config(self._jev_block(), mode="enforce")
            self._bust()
            r = tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertEqual(r[0].state, dg.ToolResultState.DENIED)
        text = r[0].content[0].text
        self.assertIn("CodeAgent", text)
        self.assertIn("该产出属于 CodeAgent 域", text)  # rubric 9: routed-owner format
        self.assertIn("命中 jev 语义仲裁 0.90", text)
        rec = self._audit()[-1]
        self.assertEqual(rec["action"], "denied")
        self._assert_jev_fields(rec["jev"], value="CodeAgent", probability=0.90,
                                fallback="")
        self.assertEqual(len(lux.requests), 1)

    def test_confident_pick_warns_in_warn_mode(self):
        with FakeSystemOne(_choice("CodeAgent", 0.90)) as lux:
            self._write_kernel({"lux": self._lux(lux.url)})
            self._write_config(self._jev_block(), mode="warn")
            self._bust()
            r = tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertNotEqual(r[0].state, dg.ToolResultState.DENIED)
        self.assertTrue(any("命中 CodeAgent" in b.text for b in r[0].content))
        rec = self._audit()[-1]
        self.assertEqual(rec["action"], "warned")
        self._assert_jev_fields(rec["jev"], value="CodeAgent", probability=0.90)

    def test_mid_confidence_warns_even_in_enforce(self):
        with FakeSystemOne(_choice("CodeAgent", 0.70)) as lux:
            self._write_kernel({"lux": self._lux(lux.url)})
            self._write_config(self._jev_block(), mode="enforce")
            self._bust()
            r = tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertNotEqual(r[0].state, dg.ToolResultState.DENIED)
        self.assertTrue(any("命中 CodeAgent" in b.text for b in r[0].content))
        rec = self._audit()[-1]
        self.assertEqual(rec["action"], "warned")
        self._assert_jev_fields(rec["jev"], value="CodeAgent", probability=0.70)

    def test_uncertain_falls_back_to_current_no_owner(self):
        with FakeSystemOne(_choice("CodeAgent", 0.50)) as lux:
            self._write_kernel({"lux": self._lux(lux.url)})
            self._write_config(self._jev_block(), mode="enforce")
            self._bust()
            r = tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertNotEqual(r[0].state, dg.ToolResultState.DENIED)
        self.assertEqual(len(r[0].content), 1, "unsure judge stays silent to the agent")
        rec = self._audit()[-1]
        self.assertEqual(rec["action"], "no_owner")
        self.assertIsNone(rec["jev"]["value"])
        self.assertEqual(rec["jev"]["probability"], 0.0)
        self.assertIn("uncertain", rec["jev"]["fallback"])

    def test_basic_op_choice_is_current_behaviour(self):
        with FakeSystemOne(_choice("basic_op", 0.95)) as lux:
            self._write_kernel({"lux": self._lux(lux.url)})
            self._write_config(self._jev_block(), mode="enforce")
            self._bust()
            r = tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertNotEqual(r[0].state, dg.ToolResultState.DENIED)
        rec = self._audit()[-1]
        self.assertEqual(rec["action"], "no_owner")
        self._assert_jev_fields(rec["jev"], value="basic_op", probability=0.95)

    def test_none_choice_is_current_behaviour(self):
        with FakeSystemOne(_choice("none", 0.95)) as lux:
            self._write_kernel({"lux": self._lux(lux.url)})
            self._write_config(self._jev_block(), mode="enforce")
            self._bust()
            tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        rec = self._audit()[-1]
        self.assertEqual(rec["action"], "no_owner")
        self.assertEqual(rec["jev"]["value"], "none")

    def test_pick_of_dead_agent_is_current_behaviour(self):
        # Judge names an agent that is not live: value not in criteria ids →
        # no arbitration, the blind spot passes as before.
        with FakeSystemOne(_choice("Ghost", 0.99)) as lux:
            self._write_kernel({"lux": self._lux(lux.url)})
            self._write_config(self._jev_block(), mode="enforce")
            self._bust()
            tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        rec = self._audit()[-1]
        self.assertEqual(rec["action"], "no_owner")

    # -- the question is the P1 shape ---------------------------------------

    def test_question_shape_is_p1(self):
        with FakeSystemOne(_choice("CodeAgent", 0.9)) as lux:
            self._write_kernel({"lux": self._lux(lux.url)})
            self._write_config(self._jev_block(), mode="enforce")
            self._bust()
            tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        body = lux.requests[0]["body"]
        criteria = body["questions"]["q0"]["criteria"]
        self.assertEqual(
            set(criteria), {"DocAgent", "CodeAgent", "basic_op", "none"}
        )
        self.assertNotIn("unrouted", criteria, "P1 uses basic_op/none, not unrouted")
        state = body["state"]
        self.assertEqual(state["target"], "outputs/report.docx")
        self.assertEqual(state["ext"], ".docx")
        self.assertEqual({a["id"] for a in state["agents"]}, {"DocAgent", "CodeAgent"})

    # -- rubric 2: latency cap fails open -----------------------------------

    def test_slow_judge_times_out_and_passes(self):
        with FakeSystemOne(sleep_s=3.0) as lux:
            self._write_kernel({"lux": self._lux(lux.url)})
            self._write_config(self._jev_block(online_timeout_ms=2500),
                               mode="enforce")
            self._bust()
            r = tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertNotEqual(r[0].state, dg.ToolResultState.DENIED)
        rec = self._audit()[-1]
        self.assertEqual(rec["action"], "no_owner")
        self.assertIn("timeout", rec["jev"]["fallback"])
        self.assertLess(rec["jev"]["latencyMs"], 3500)

    def test_lux_down_fails_open_no_owner(self):
        with FakeSystemOne() as dead:
            url = dead.url
        self._write_kernel({"lux": self._lux(url)})
        self._write_config(self._jev_block(), mode="enforce")
        self._bust()
        r = tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertNotEqual(r[0].state, dg.ToolResultState.DENIED)
        rec = self._audit()[-1]
        self.assertEqual(rec["action"], "no_owner")
        self.assertIn("error:", rec["jev"]["fallback"])

    def test_configured_but_unrunnable_audits_fallback(self):
        # Enabled, but no lux spec in the kernel config → no_judge_configured.
        self._write_kernel({"clef": self._lux("http://127.0.0.1:1")})
        self._write_config(self._jev_block(), mode="enforce")
        self._bust()
        r = tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertNotEqual(r[0].state, dg.ToolResultState.DENIED)
        rec = self._audit()[-1]
        self.assertEqual(rec["action"], "no_owner")
        self._assert_jev_fields(rec["jev"], value=None, probability=0.0,
                                fallback="no_judge_configured", judge=None)

    # -- rubric 3: L1 route hit pays no judge -------------------------------

    def test_l1_route_hit_never_asks_the_judge(self):
        with FakeSystemOne(_choice("DocAgent", 0.9)) as lux:
            self._write_kernel({"lux": self._lux(lux.url)})
            self._write_config(self._jev_block(), mode="enforce",
                               ext_routes={".docx": "DocAgent"})
            self._bust()
            r = tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertEqual(r[0].state, dg.ToolResultState.DENIED)
        self.assertEqual(lux.requests, [], "a live route hit never pays judge latency")
        self.assertNotIn("jev", self._audit()[-1], "normal deny path carries no jev")

    # -- rubric 4: third branch (unreadable inventory) is never arbitrated ---

    def test_unreadable_inventory_escalates_with_zero_requests(self):
        bad = self.ws / "agents" / "DocAgent" / "agent.json"
        os.chmod(bad, 0)
        try:
            with FakeSystemOne(_choice("CodeAgent", 0.9)) as lux:
                self._write_kernel({"lux": self._lux(lux.url)})
                self._write_config(self._jev_block(), mode="enforce")
                self._bust()
                r = tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        finally:
            os.chmod(bad, 0o644)
        self.assertEqual(r[0].state, dg.ToolResultState.DENIED)
        self.assertIn("清单读取失败", r[0].content[0].text)
        self.assertEqual(lux.requests, [], "the read-failure branch is never arbitrated")

    # -- rubric 5: disabled = byte-identical, never dials out ---------------

    def test_disabled_never_dials_out_and_stays_byte_identical(self):
        # The judge's only network primitive is jev._post_json; a tripwire
        # there proves jev-off issues zero outbound requests. (Mocking the
        # global socket.socket instead would break asyncio, whose event loop
        # tm.run builds via socket.socketpair while the middleware runs.)
        self._write_config({"enabled": False}, mode="enforce")
        self._bust()
        with mock.patch.object(
            dg._jev,
            "_post_json",
            side_effect=AssertionError("judge dialed while jev off"),
        ):
            r = tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertNotEqual(r[0].state, dg.ToolResultState.DENIED)
        rec = self._audit()[-1]
        self.assertEqual(rec["action"], "no_owner")
        self.assertNotIn("jev", rec, "off is byte-identical: no jev subobject")

    def test_absent_jev_block_is_byte_identical(self):
        self._write_config(None, mode="enforce")
        self._bust()
        with mock.patch.object(
            dg._jev,
            "_post_json",
            side_effect=AssertionError("judge dialed with no jev block"),
        ):
            r = tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertNotEqual(r[0].state, dg.ToolResultState.DENIED)
        rec = self._audit()[-1]
        self.assertEqual(rec["action"], "no_owner")
        self.assertNotIn("jev", rec)

    # -- audit toggle -------------------------------------------------------

    def test_record_false_suppresses_jev_subobject_but_still_gates(self):
        with FakeSystemOne(_choice("CodeAgent", 0.9)) as lux:
            self._write_kernel({"lux": self._lux(lux.url)})
            self._write_config(self._jev_block(record=False), mode="enforce")
            self._bust()
            r = tm.run(self.mw, "write_file", {"file_path": "outputs/report.docx"})
        self.assertEqual(r[0].state, dg.ToolResultState.DENIED)  # gate still applies
        self.assertNotIn("jev", self._audit()[-1], "record:false writes no jev")

    # -- helper -------------------------------------------------------------

    @staticmethod
    def _assert_jev_fields(jev, *, value, probability, fallback=None, judge="lux"):
        assert set(jev) == {"judge", "latencyMs", "probability", "value", "fallback"}, jev
        assert jev["value"] == value, jev
        assert jev["probability"] == probability, jev
        assert jev["judge"] == judge, jev
        assert isinstance(jev["latencyMs"], (int, float)), jev
        if fallback is not None:
            assert jev["fallback"] == fallback, jev


if __name__ == "__main__":
    unittest.main()
