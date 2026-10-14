#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 tests: the jev layer's hook into draft generation (_draft_route_table).

Rubric coverage (see docs/JEV_INTEGRATION.md §5):
2. latency cap — a judge slower than online_timeout_ms falls back to the
   keyword scan with an audited fallback=timeout;
3. inherit resolution — missing kernel config / upstream kill-switch / a
   whitelisted tier without a spec all disable the layer with an audited
   reason, and a ["lux"] whitelist never dials clef/session specs;
4. gating — p=0.7 routes to the named owner, p=0.5 and "unrouted" fall back
   to the keyword scan;
5. D7 — writing the guard's own table is denied by the meta-integrity gate
   with ZERO judge requests;
6. regression — jev off makes zero outbound sockets;
7. privacy — an enabled inherit chain dials only the whitelisted tier's
   endpoint;
8. lux down (refused / 404) — the draft falls back to the keyword scan with
   an audited reason and no exception.
"""
import asyncio  # noqa: F401  (used via test_middleware.run)
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "tests"))

import main as dg  # noqa: E402
from test_jev_client import FakeSystemOne  # noqa: E402


def _write_agent(ws: Path, agent_id: str, skills=(), description=""):
    d = ws / "agents" / agent_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.json").write_text(
        json.dumps({"id": agent_id, "skills": list(skills), "description": description}),
        encoding="utf-8",
    )


class JevDraftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        self.plugin_tmp = tempfile.TemporaryDirectory()
        self.plugin_tree = Path(self.plugin_tmp.name)
        self.saved = {
            "config": dict(dg._CONFIG_CACHE),
            "plugin_dir": dg.PLUGIN_DIR,
            "agents": dict(dg._AGENTS_CACHE),
            "draft": dg._DRAFT_WRITTEN,
            "unconfigured": dg._UNCONFIGURED_LOGGED,
            "env": os.environ.get("JEV_KERNEL_CONFIG"),
        }
        dg.PLUGIN_DIR = self.plugin_tree
        self.kernel_cfg = Path(self.tmp.name) / "jev.json"
        os.environ["JEV_KERNEL_CONFIG"] = str(self.kernel_cfg)
        # Two specialists whose manifest blobs decide keyword-scan fallback:
        # DocAgent matches the document preset, OpsAgent matches archives;
        # nobody mentions hardware, so that preset is the scan's blind spot.
        _write_agent(self.ws, "DocAgent", ["doc"], "writes documents and reports")
        _write_agent(self.ws, "OpsAgent", ["ops"], "release and deploy operations")

    def tearDown(self):
        dg._CONFIG_CACHE.clear()
        dg._CONFIG_CACHE.update(self.saved["config"])
        dg._AGENTS_CACHE.clear()
        dg._AGENTS_CACHE.update(self.saved["agents"])
        dg.PLUGIN_DIR = self.saved["plugin_dir"]
        dg._DRAFT_WRITTEN = self.saved["draft"]
        dg._UNCONFIGURED_LOGGED = self.saved["unconfigured"]
        if self.saved["env"] is None:
            os.environ.pop("JEV_KERNEL_CONFIG", None)
        else:
            os.environ["JEV_KERNEL_CONFIG"] = self.saved["env"]
        self.tmp.cleanup()
        self.plugin_tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def _write_config(self, jev_block):
        cfg = {"mode": "warn", "routes": [], "ext_routes": {}, "jev": jev_block}
        (self.plugin_tree / "routes.json").write_text(
            json.dumps(cfg), encoding="utf-8"
        )
        dg._CONFIG_CACHE.update({"mtime": 0.0, "data": None, "source": None})

    def _write_kernel(self, judges, **over):
        cfg = {"judges": judges}
        cfg.update(over)
        self.kernel_cfg.write_text(json.dumps(cfg), encoding="utf-8")

    def _jev_block(self, **over):
        block = {"enabled": True, "judges": "inherit:jev-kernel", "tiers": ["lux"]}
        block.update(over)
        return block

    @staticmethod
    def _lux_spec(url):
        return {
            "type": "systemone",
            "baseUrl": url,
            "model": "judge-model",
            "timeoutMs": 3000,
        }

    def _draft(self):
        audit: list[dict] = []
        cfg = dg._draft_route_table(self.ws, jev_audit=audit)
        return cfg, audit

    # -- rubric 4: gating ----------------------------------------------------

    def test_certain_lux_answer_routes_owner(self):
        # p=0.7 >= warn(0.6) → the judge's owner wins for that category; the
        # hardware category (a keyword blind spot) gets PCB_Agent from lux.
        def responder(body):
            return {"answers": {
                "q0": {"type": "choice", "choice": "DocAgent",
                       "probabilities": {"DocAgent": 0.7}},
                "q1": {"type": "choice", "choice": "unrouted",
                       "probabilities": {"unrouted": 0.9}},
                "q2": {"type": "choice", "choice": "PCB_Agent",
                       "probabilities": {"PCB_Agent": 0.95}},
                "q3": {"type": "choice", "choice": "Ghost",
                       "probabilities": {"Ghost": 0.99}},
            }}

        with FakeSystemOne(responder) as lux:
            self._write_kernel({"lux": self._lux_spec(lux.url)})
            self._write_config(self._jev_block())
            # PCB_Agent must be live for the judge's pick to count.
            _write_agent(self.ws, "PCB_Agent", [], "circuit boards")
            dg._AGENTS_CACHE.update({"ws": None, "agents": None, "ids": frozenset(), "stamp": None})
            cfg, audit = self._draft()
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")
        self.assertEqual(cfg["ext_routes"][".kicad_pcb"], "PCB_Agent")
        # q1 unrouted → keyword scan (no match for media terms → unrouted);
        # q3 named a non-live agent → scan. Neither is routed.
        self.assertNotIn(".png", cfg["ext_routes"])
        # q3 named a ghost → the keyword scan decides, and it finds OpsAgent.
        self.assertEqual(cfg["ext_routes"][".zip"], "OpsAgent")
        self.assertEqual(audit[0]["value"]["q2"], "PCB_Agent")
        self.assertEqual(audit[0]["probability"]["q2"], 0.95)
        self.assertEqual(audit[0]["judge"], "lux")
        self.assertEqual(audit[0]["fallback"], "lux:malformed:q3",
                         "the ghost answer is the only rejected verdict")
        self.assertEqual(len(lux.requests), 1, "one request, all categories")

    def test_uncertain_answer_falls_back_to_keyword_scan(self):
        # p=0.5 < 0.6 → uncertain → the document category falls to the
        # keyword scan, which finds DocAgent; lux's pick is overridden.
        def responder(body):
            return {"answers": {
                "q0": {"type": "choice", "choice": "OpsAgent",
                       "probabilities": {"OpsAgent": 0.5}},
            }}

        with FakeSystemOne(responder) as lux:
            self._write_kernel({"lux": self._lux_spec(lux.url)})
            self._write_config(self._jev_block())
            cfg, audit = self._draft()
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent",
                         "an unsure judge yields the keyword scan's owner")
        self.assertIsNone(audit[0]["value"]["q0"])
        self.assertIn("uncertain", audit[0]["fallback"])

    # -- rubric 2: latency cap -----------------------------------------------

    def test_slow_judge_times_out_and_falls_back(self):
        with FakeSystemOne(sleep_s=3.0) as lux:
            self._write_kernel({"lux": self._lux_spec(lux.url)})
            self._write_config(self._jev_block(online_timeout_ms=2500))
            cfg, audit = self._draft()
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")
        self.assertEqual(cfg["ext_routes"][".zip"], "OpsAgent")
        self.assertIn("timeout", audit[0]["fallback"])
        self.assertLess(audit[0]["latencyMs"], 3500)

    # -- rubric 3: inherit resolution ----------------------------------------

    def test_missing_kernel_config_disables_with_audit(self):
        os.environ["JEV_KERNEL_CONFIG"] = str(self.plugin_tree / "absent.json")
        self._write_config(self._jev_block())
        cfg, audit = self._draft()
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")
        self.assertEqual(audit[0]["fallback"], "no_judge_configured")
        self.assertIsNone(audit[0]["judge"])

    def test_kernel_kill_switch_disables_with_audit(self):
        self._write_kernel({}, enabled=False)
        self._write_config(self._jev_block())
        cfg, audit = self._draft()
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")
        self.assertEqual(audit[0]["fallback"], "disabled_upstream")

    def test_whitelisted_tier_without_spec_disables_with_audit(self):
        self._write_kernel({"clef": self._lux_spec("http://127.0.0.1:1")})
        self._write_config(self._jev_block())
        cfg, audit = self._draft()
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")
        self.assertEqual(audit[0]["fallback"], "no_judge_configured")

    def test_lux_whitelist_never_dials_other_tiers(self):
        # Rubric 3+7: clef and session specs exist but tiers:["lux"] — their
        # servers must see zero requests; only the lux fake is dialed.
        def responder(body):
            return {"answers": {"q0": {"type": "choice", "choice": "DocAgent",
                                       "probabilities": {"DocAgent": 0.9}}}}

        with FakeSystemOne(responder) as lux, FakeSystemOne() as clef, FakeSystemOne() as sess:
            self._write_kernel({
                "lux": self._lux_spec(lux.url),
                "clef": self._lux_spec(clef.url),
                "session": {"type": "llm", "baseUrl": sess.url},
            })
            self._write_config(self._jev_block())
            cfg, _ = self._draft()
        self.assertGreaterEqual(len(lux.requests), 1)
        self.assertEqual(clef.requests, [])
        self.assertEqual(sess.requests, [])
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")

    # -- rubric 8: lux down ---------------------------------------------------

    def test_lux_connection_refused_falls_back(self):
        with FakeSystemOne() as dead:
            url = dead.url
        self._write_kernel({"lux": self._lux_spec(url)})
        self._write_config(self._jev_block())
        cfg, audit = self._draft()  # must not raise
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")
        self.assertIn("error:", audit[0]["fallback"])

    def test_lux_404_falls_back(self):
        with FakeSystemOne(status=404) as lux:
            self._write_kernel({"lux": self._lux_spec(lux.url)})
            self._write_config(self._jev_block())
            cfg, audit = self._draft()
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")
        self.assertTrue(audit[0]["fallback"])

    # -- rubric 6: off = zero sockets ----------------------------------------

    def test_disabled_jev_opens_no_sockets(self):
        self._write_config({"enabled": False})
        with mock.patch.object(
            socket, "socket", side_effect=AssertionError("outbound socket while jev off")
        ):
            cfg, audit = self._draft()
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")
        self.assertEqual(audit, [], "off is silent: no jev audit record")

    def test_absent_jev_block_is_byte_identical_off(self):
        (self.plugin_tree / "routes.json").write_text(
            json.dumps({"mode": "warn", "routes": [], "ext_routes": {}}), encoding="utf-8"
        )
        dg._CONFIG_CACHE.update({"mtime": 0.0, "data": None, "source": None})
        with mock.patch.object(
            socket, "socket", side_effect=AssertionError("outbound socket with no jev block")
        ):
            cfg, audit = self._draft()
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")
        self.assertEqual(audit, [])

    # -- audit plumbing through _maybe_write_draft -----------------------------

    def test_maybe_write_draft_logs_jev_record(self):
        def responder(body):
            return {"answers": {"q0": {"type": "choice", "choice": "DocAgent",
                                       "probabilities": {"DocAgent": 0.9}}}}

        records: list[dict] = []
        with FakeSystemOne(responder) as lux:
            self._write_kernel({"lux": self._lux_spec(lux.url)})
            self._write_config(self._jev_block())
            path = dg._maybe_write_draft(self.ws, log=records.append)
        self.assertIsNotNone(path)
        jev_records = [r for r in records if r.get("jev")]
        self.assertEqual(len(jev_records), 1)
        self.assertEqual(jev_records[0]["jev"]["value"]["q0"], "DocAgent")
        self.assertEqual(jev_records[0]["action"], "draft")

    def test_record_false_writes_no_jev_audit(self):
        def responder(body):
            return {"answers": {"q0": {"type": "choice", "choice": "DocAgent",
                                       "probabilities": {"DocAgent": 0.9}}}}

        records: list[dict] = []
        with FakeSystemOne(responder) as lux:
            self._write_kernel({"lux": self._lux_spec(lux.url)})
            self._write_config(self._jev_block(record=False))
            dg._maybe_write_draft(self.ws, log=records.append)
        self.assertEqual([r for r in records if r.get("jev")], [])

    def test_policy_still_wins_and_skips_the_judge(self):
        # The orchestrator's declared "X派Y" policy outranks the judge; with a
        # policy present the judge is not even asked.
        (self.ws / "agent.json").write_text(
            json.dumps({"id": "default",
                        "description": "文档/报告派 DocAgent，代码派 OpsAgent"}),
            encoding="utf-8",
        )
        dg._AGENTS_CACHE.update({"ws": None, "agents": None, "ids": frozenset(), "stamp": None})
        with FakeSystemOne() as lux:
            self._write_kernel({"lux": self._lux_spec(lux.url)})
            self._write_config(self._jev_block())
            cfg, _ = self._draft()
        self.assertEqual(cfg["ext_routes"][".docx"], "DocAgent")
        self.assertEqual(lux.requests, [], "a declared policy never pays judge latency")


class JevMetaIntegrityTest(unittest.TestCase):
    """Rubric 5 (D7): a write to the guard's own table is D1-denied with the
    judge enabled, and the judge sees ZERO requests — jev never bypasses (or
    pays latency for) the meta-integrity gate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        (self.ws / "logs").mkdir()
        self.plugin_tmp = tempfile.TemporaryDirectory()
        self.plugin_tree = Path(self.plugin_tmp.name)
        self.saved = {
            "config": dict(dg._CONFIG_CACHE),
            "plugin_dir": dg.PLUGIN_DIR,
            "agents": dict(dg._AGENTS_CACHE),
            "off": dg._OFF_LOGGED,
            "env": os.environ.get("JEV_KERNEL_CONFIG"),
        }
        dg.PLUGIN_DIR = self.plugin_tree

    def tearDown(self):
        dg._CONFIG_CACHE.clear()
        dg._CONFIG_CACHE.update(self.saved["config"])
        dg._AGENTS_CACHE.clear()
        dg._AGENTS_CACHE.update(self.saved["agents"])
        dg.PLUGIN_DIR = self.saved["plugin_dir"]
        dg._OFF_LOGGED = self.saved["off"]
        if self.saved["env"] is None:
            os.environ.pop("JEV_KERNEL_CONFIG", None)
        else:
            os.environ["JEV_KERNEL_CONFIG"] = self.saved["env"]
        self.tmp.cleanup()
        self.plugin_tmp.cleanup()

    def test_guard_table_write_denied_with_zero_judge_requests(self):
        import test_middleware as tm

        kernel_cfg = Path(self.tmp.name) / "jev.json"
        os.environ["JEV_KERNEL_CONFIG"] = str(kernel_cfg)
        with FakeSystemOne() as lux:
            kernel_cfg.write_text(json.dumps({"judges": {"lux": {
                "type": "systemone", "baseUrl": lux.url, "model": "m", "timeoutMs": 3000,
            }}}), encoding="utf-8")
            (self.plugin_tree / "routes.json").write_text(json.dumps({
                "mode": "enforce", "routes": [], "ext_routes": {},
                "jev": {"enabled": True, "judges": "inherit:jev-kernel", "tiers": ["lux"]},
            }), encoding="utf-8")
            dg._CONFIG_CACHE.update({"mtime": 0.0, "data": None, "source": None})
            mw = dg.DispatchGuardMiddleware(str(self.ws), "enforce")
            events = tm.run(mw, "write_file", {
                "file_path": str(self.plugin_tree / "routes.json"),
                "content": "{}",
            })
            self.assertEqual(events[0].state, dg.ToolResultState.DENIED)
            self.assertEqual(lux.requests, [], "D1 sits above the judge: zero requests")


if __name__ == "__main__":
    unittest.main()
