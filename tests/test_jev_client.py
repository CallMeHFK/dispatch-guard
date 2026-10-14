#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Contract tests for dispatch-guard's jev wire client (backend/jev.py).

The client is a deliberate stdlib mirror of jev-kernel's raw ``systemone``
dialect (backend/judges.py). These tests pin the mirror against a real fake
HTTP server (stdlib http.server) so protocol drift upstream shows up here
instead of in production: baseUrl normalisation, boolean->noul translation,
score->argmax, the ordered answers-list zip, and the _parse_error diagnostic.

Stdlib-only, like the plugin itself.
"""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

import jev  # noqa: E402


class FakeSystemOne:
    """A controllable in-process systemone server that records requests."""

    def __init__(self, responder=None, sleep_s=0.0, status=200):
        self.requests: list[dict] = []
        self.responder = responder or (lambda body: {"answers": {}})
        self.sleep_s = sleep_s
        self.status = status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"path": self.path, "body": body})
                if outer.sleep_s:
                    import time

                    time.sleep(outer.sleep_s)
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    return
                payload = json.dumps(outer.responder(body)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client may have timed out and hung up first

            def log_message(self, *args):  # silence test output
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _spec(url, **over):
    spec = {"name": "lux", "type": "systemone", "baseUrl": url, "model": "m", "timeoutMs": 3000}
    spec.update(over)
    return spec


def _runtime(specs, timeout_ms=2500, warn=0.6):
    return jev.Runtime(specs, {"deny": 0.85, "warn": warn}, timeout_ms, True)


class WireShapeTest(unittest.TestCase):
    """The five mirrored protocol behaviours, pinned end to end."""

    def test_baseurl_normalization_bare(self):
        with FakeSystemOne() as fake:
            jev._judge_sync(_spec(fake.url), {}, {"q0": {"type": "boolean"}}, 1000)
        self.assertEqual(fake.requests[0]["path"], "/v1/systemone")

    def test_baseurl_normalization_trailing_slash(self):
        with FakeSystemOne() as fake:
            jev._judge_sync(_spec(fake.url + "/"), {}, {"q0": {"type": "boolean"}}, 1000)
        self.assertEqual(fake.requests[0]["path"], "/v1/systemone")

    def test_baseurl_normalization_v1_suffix(self):
        with FakeSystemOne() as fake:
            jev._judge_sync(_spec(fake.url + "/v1"), {}, {"q0": {"type": "boolean"}}, 1000)
        self.assertEqual(fake.requests[0]["path"], "/v1/systemone")

    def test_baseurl_other_path_used_as_is(self):
        with FakeSystemOne() as fake:
            jev._judge_sync(_spec(fake.url + "/v1/systemone"), {}, {}, 1000)
        self.assertEqual(fake.requests[0]["path"], "/v1/systemone")

    def test_boolean_out_as_noul_and_answer_translated(self):
        def responder(body):
            self.assertEqual(body["questions"]["q0"]["type"], "noul")
            return {"answers": {"q0": {"type": "noul", "noul": 0.9}}}

        with FakeSystemOne(responder) as fake:
            answers = jev._judge_sync(
                _spec(fake.url), {}, {"q0": {"type": "boolean", "instructions": "?"}}, 1000
            )
        self.assertEqual(answers["q0"], {"type": "boolean", "probability": 0.9})

    def test_choice_options_become_criteria_map(self):
        def responder(body):
            q = body["questions"]["q0"]
            self.assertNotIn("options", q)
            self.assertEqual(q["criteria"], {"a": "a", "b": "b"})
            return {"answers": {"q0": {"type": "choice", "choice": "b",
                                       "probabilities": {"a": 0.2, "b": 0.8}}}}

        with FakeSystemOne(responder) as fake:
            answers = jev._judge_sync(
                _spec(fake.url),
                {},
                {"q0": {"type": "choice", "instructions": "?", "options": ["a", "b"]}},
                1000,
            )
        self.assertEqual(answers["q0"]["choice"], "b")

    def test_choice_criteria_dict_passes_through(self):
        def responder(body):
            q = body["questions"]["q0"]
            self.assertEqual(q["criteria"], {"a": "alpha", "b": "beta"})
            return {"answers": {"q0": {"type": "choice", "choice": "a",
                                       "probabilities": {"a": 0.9}}}}

        with FakeSystemOne(responder) as fake:
            answers = jev._judge_sync(
                _spec(fake.url),
                {},
                {"q0": {"type": "choice", "instructions": "?",
                        "criteria": {"a": "alpha", "b": "beta"}}},
                1000,
            )
        self.assertEqual(answers["q0"]["choice"], "a")

    def test_score_argmax_from_probabilities(self):
        def responder(body):
            # per-level probabilities are keyed by level INDEX (upstream
            # judges.py semantics), not by criteria label.
            return {"answers": {"q0": {"type": "score",
                                       "probabilities": {"0": 0.1, "1": 0.7, "2": 0.2}}}}

        with FakeSystemOne(responder) as fake:
            answers = jev._judge_sync(
                _spec(fake.url),
                {},
                {"q0": {"type": "score", "instructions": "?", "criteria": ["0", "5", "10"]}},
                1000,
            )
        self.assertEqual(answers["q0"]["type"], "score")
        self.assertEqual(answers["q0"]["score"], 1)
        self.assertEqual(answers["q0"]["confidence"], 0.7)

    def test_score_falls_back_to_rounded_score(self):
        with FakeSystemOne(
            responder=lambda body: {"answers": {"q0": {"type": "score", "score": 1.6}}}
        ) as fake:
            answers = jev._judge_sync(
                _spec(fake.url),
                {},
                {"q0": {"type": "score", "instructions": "?", "criteria": ["0", "5", "10"]}},
                1000,
            )
        self.assertEqual(answers["q0"]["score"], 2)

    def test_answers_ordered_list_zips_to_question_keys(self):
        with FakeSystemOne(
            responder=lambda body: {"answers": [
                {"type": "noul", "noul": 0.9},
                {"type": "choice", "choice": "a", "probabilities": {"a": 0.9}},
            ]}
        ) as fake:
            answers = jev._judge_sync(
                _spec(fake.url),
                {},
                {
                    "q0": {"type": "boolean", "instructions": "?"},
                    "q1": {"type": "choice", "instructions": "?", "criteria": {"a": "a"}},
                },
                1000,
            )
        self.assertEqual(answers["q0"], {"type": "boolean", "probability": 0.9})
        self.assertEqual(answers["q1"]["choice"], "a")

    def test_unparseable_payload_becomes_parse_error(self):
        with FakeSystemOne(responder=lambda body: {"totally": "unexpected"}) as fake:
            answers = jev._judge_sync(_spec(fake.url), {}, {"q0": {"type": "boolean"}}, 1000)
        self.assertIn("_parse_error", answers)

    def test_empty_payload_is_not_a_parse_error(self):
        with FakeSystemOne(responder=lambda body: {}) as fake:
            answers = jev._judge_sync(_spec(fake.url), {}, {"q0": {"type": "boolean"}}, 1000)
        self.assertEqual(answers, {})


class AskTest(unittest.TestCase):
    """ask(): gating, cascade-of-one, and the failure fallbacks."""

    def test_certain_answer_resolves(self):
        with FakeSystemOne(
            responder=lambda body: {"answers": {"q0": {"type": "choice", "choice": "DocAgent",
                                                       "probabilities": {"DocAgent": 0.9}}}}
        ) as fake:
            verdicts, audit = jev.ask(
                _runtime([_spec(fake.url)]),
                {},
                {"q0": {"type": "choice", "criteria": {"DocAgent": "d", "unrouted": "u"}}},
            )
        v = verdicts["q0"]
        self.assertEqual((v["value"], v["probability"], v["uncertain"], v["fallback"]),
                         ("DocAgent", 0.9, False, ""))
        self.assertEqual(v["judge"], "lux")
        self.assertEqual(audit["judge"], "lux")
        self.assertGreaterEqual(audit["latencyMs"], 0)

    def test_uncertain_answer_falls_back(self):
        with FakeSystemOne(
            responder=lambda body: {"answers": {"q0": {"type": "choice", "choice": "DocAgent",
                                                       "probabilities": {"DocAgent": 0.5}}}}
        ) as fake:
            verdicts, _ = jev.ask(
                _runtime([_spec(fake.url)]),
                {},
                {"q0": {"type": "choice", "criteria": {"DocAgent": "d", "unrouted": "u"}}},
            )
        self.assertTrue(verdicts["q0"]["uncertain"])
        self.assertIsNone(verdicts["q0"]["value"])

    def test_malformed_answer_falls_back(self):
        with FakeSystemOne(
            responder=lambda body: {"answers": {"q0": {"type": "choice", "choice": "Ghost"}}}
        ) as fake:
            verdicts, _ = jev.ask(
                _runtime([_spec(fake.url)]),
                {},
                {"q0": {"type": "choice", "criteria": {"DocAgent": "d"}}},
            )
        self.assertIn("malformed", verdicts["q0"]["fallback"])

    def test_http_error_status_falls_back_without_raising(self):
        with FakeSystemOne(status=404) as fake:
            verdicts, _ = jev.ask(
                _runtime([_spec(fake.url)]),
                {},
                {"q0": {"type": "choice", "criteria": {"a": "a"}}},
            )
        self.assertTrue(verdicts["q0"]["uncertain"])
        self.assertIn("error:", verdicts["q0"]["fallback"])

    def test_connection_refused_falls_back_without_raising(self):
        with FakeSystemOne() as fake:
            url = fake.url  # closed below: nothing listens
        verdicts, _ = jev.ask(
            _runtime([_spec(url, timeoutMs=500)]),
            {},
            {"q0": {"type": "choice", "criteria": {"a": "a"}}},
        )
        self.assertIn("error:", verdicts["q0"]["fallback"])

    def test_timeout_cap_falls_back_as_timeout(self):
        # The fake sleeps 3s; the online cap is 2500ms (min'ed against the
        # spec's own 3000ms), so the client must give up first.
        with FakeSystemOne(sleep_s=3.0) as fake:
            verdicts, audit = jev.ask(
                _runtime([_spec(fake.url)], timeout_ms=2500),
                {},
                {"q0": {"type": "choice", "criteria": {"a": "a"}}},
            )
        self.assertEqual(verdicts["q0"]["fallback"], "lux:timeout")
        self.assertLess(audit["latencyMs"], 3000)


class ResolveTest(unittest.TestCase):
    """inherit:jev-kernel resolution order, kill-switch and tier whitelist."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin_dir = Path(self.tmp.name) / "plugins" / "dispatch-guard"
        self.plugin_dir.mkdir(parents=True)
        self.kernel_plugin = self.plugin_dir.parent / "jev-kernel"
        self.kernel_data = (
            Path(self.tmp.name) / "plugin-data" / "jev-kernel"
        )
        self._saved_env = None

    def tearDown(self):
        import os

        if self._saved_env is None:
            os.environ.pop("JEV_KERNEL_CONFIG", None)
        else:
            os.environ["JEV_KERNEL_CONFIG"] = self._saved_env

    def _write_kernel(self, path: Path, cfg: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg), encoding="utf-8")

    def _cfg(self, **jev_over):
        block = {"enabled": True, "judges": "inherit:jev-kernel", "tiers": ["lux"]}
        block.update(jev_over)
        return {"mode": "warn", "jev": block}

    def test_absent_block_is_silently_off(self):
        runtime, reason, record = jev.resolve({"mode": "warn"}, self.plugin_dir)
        self.assertIsNone(runtime)
        self.assertIsNone(reason)

    def test_disabled_block_is_silently_off(self):
        runtime, reason, record = jev.resolve(
            {"jev": {"enabled": False}}, self.plugin_dir
        )
        self.assertIsNone(runtime)
        self.assertIsNone(reason)

    def test_missing_kernel_config_disables_with_reason(self):
        runtime, reason, record = jev.resolve(self._cfg(), self.plugin_dir)
        self.assertIsNone(runtime)
        self.assertEqual(reason, "no_judge_configured")
        self.assertTrue(record)

    def test_kernel_kill_switch_disables(self):
        self._write_kernel(self.kernel_plugin / "jev.json", {"enabled": False})
        runtime, reason, _ = jev.resolve(self._cfg(), self.plugin_dir)
        self.assertIsNone(runtime)
        self.assertEqual(reason, "disabled_upstream")

    def test_tier_without_spec_disables(self):
        self._write_kernel(self.kernel_plugin / "jev.json", {"judges": {"clef": {}}})
        runtime, reason, _ = jev.resolve(self._cfg(), self.plugin_dir)
        self.assertIsNone(runtime)
        self.assertEqual(reason, "no_judge_configured")

    def test_plugin_dir_wins_over_plugin_data(self):
        self._write_kernel(
            self.kernel_data / "jev.json",
            {"judges": {"lux": {"baseUrl": "http://10.0.0.2:1", "model": "data"}}},
        )
        self._write_kernel(
            self.kernel_plugin / "jev.json",
            {"judges": {"lux": {"baseUrl": "http://10.0.0.1:1", "model": "plugin"}}},
        )
        runtime, reason, _ = jev.resolve(self._cfg(), self.plugin_dir)
        self.assertIsNone(reason)
        self.assertEqual(runtime.specs[0]["baseUrl"], "http://10.0.0.1:1")

    def test_plugin_data_is_the_fallback(self):
        self._write_kernel(
            self.kernel_data / "jev.json",
            {"judges": {"lux": {"baseUrl": "http://10.0.0.2:1", "model": "data"}}},
        )
        runtime, _, _ = jev.resolve(self._cfg(), self.plugin_dir)
        self.assertEqual(runtime.specs[0]["model"], "data")

    def test_tier_whitelist_excludes_other_judges(self):
        self._write_kernel(
            self.kernel_plugin / "jev.json",
            {"judges": {
                "lux": {"baseUrl": "http://10.0.0.1:1"},
                "clef": {"baseUrl": "http://10.0.0.3:1"},
            }},
        )
        runtime, _, _ = jev.resolve(self._cfg(), self.plugin_dir)
        self.assertEqual([s["name"] for s in runtime.specs], ["lux"])

    def test_inline_judges_and_thresholds(self):
        cfg = self._cfg(
            judges=[{"name": "lab", "type": "systemone",
                     "baseUrl": "http://10.0.0.9:9", "model": "m", "timeoutMs": 900}],
            thresholds={"deny": 0.9, "warn": 0.5},
        )
        runtime, reason, _ = jev.resolve(cfg, self.plugin_dir)
        self.assertIsNone(reason)
        self.assertEqual(runtime.specs[0]["name"], "lab")
        self.assertEqual(runtime.thresholds, {"deny": 0.9, "warn": 0.5})

    def test_inline_empty_disables_with_reason(self):
        runtime, reason, _ = jev.resolve(self._cfg(judges=[]), self.plugin_dir)
        self.assertIsNone(runtime)
        self.assertEqual(reason, "no_judge_configured")

    def test_wrong_typed_block_degrades_without_crash(self):
        for bad in (["jev"], "jev", 5, True):
            runtime, reason, _ = jev.resolve({"jev": bad}, self.plugin_dir)
            self.assertIsNone(runtime)
            self.assertIsNone(reason)

    def test_wrong_typed_fields_degrade_to_defaults(self):
        self._write_kernel(
            self.kernel_plugin / "jev.json",
            {"judges": {"lux": {"baseUrl": "http://10.0.0.1:1"}}},
        )
        cfg = self._cfg(online_timeout_ms="fast", record="yes", tiers="lux")
        runtime, reason, record = jev.resolve(cfg, self.plugin_dir)
        self.assertIsNone(reason)
        self.assertEqual(runtime.timeout_ms, 2500)
        self.assertTrue(record)
        self.assertEqual([s["name"] for s in runtime.specs], ["lux"])

    def test_record_false_is_honored(self):
        self._write_kernel(
            self.kernel_plugin / "jev.json",
            {"judges": {"lux": {"baseUrl": "http://10.0.0.1:1"}}},
        )
        _, _, record = jev.resolve(self._cfg(record=False), self.plugin_dir)
        self.assertFalse(record)


if __name__ == "__main__":
    unittest.main()
