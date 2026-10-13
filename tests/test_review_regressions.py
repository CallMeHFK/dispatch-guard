#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for the 2026-10-12 adversarial review (review-20261012.md).

One test per fixed finding. Stdlib-only, same conventions as
test_middleware.py (temp plugin tree outside the temp workspace, real
routes.json files, global caches saved and restored).
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
        self.saved_off_logged = dg._OFF_LOGGED
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
        dg._OFF_LOGGED = self.saved_off_logged
        self.tmp.cleanup()
        self.plugin_tmp.cleanup()

    def _bust(self):
        dg._CONFIG_CACHE.clear()
        dg._CONFIG_CACHE.update({"mtime": 0.0, "data": None, "source": None})
        dg._UNCONFIGURED_LOGGED = False
        dg._AGENTS_CACHE.update({"ws": None, "agents": None, "ids": frozenset()})
        dg._DRAFT_WRITTEN = False
        dg._NO_OWNER_LOGGED = False
        dg._OFF_LOGGED = False

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

    # -- D4: malformed routes.json fields degrade, never crash --------------

    def test_null_match_rule_degrades_gracefully(self):
        # review D4: {"match": null} raised TypeError out of _route_hit on
        # every deliverable interception. After the fix the rule degrades to
        # empty keywords — the class it meant to claim is unrouted, so the
        # write passes as no_owner (a malformed table must not become a
        # blanket blocker).
        self._write_config(
            {
                "mode": "enforce",
                "routes": [{"match": None, "agent": "DocAgent"}],
                "ext_routes": {},
            }
        )
        with self.assertLogs("dispatch_guard.qwenpaw", level="WARNING") as logs:
            r = run(self.mw, "write_file", {"file_path": "outputs/a.docx"})
        self.assertNotEqual(r[0].state, "denied")
        self.assertTrue(any("null 'match'" in line for line in logs.output))
        audit = Path(self.ws) / "logs" / "dispatch_guard.jsonl"
        actions = [
            json.loads(line)["action"]
            for line in audit.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn("no_owner", actions)
        # ...and healthy rules in the same table (plus coerced non-string
        # keywords) keep routing.
        self._write_config(
            {
                "mode": "enforce",
                "routes": [
                    {"match": None, "agent": "DocAgent"},
                    {"match": [123, "报告"], "agent": "DocAgent"},
                ],
                "ext_routes": {},
            }
        )
        r2 = run(self.mw, "write_file", {"file_path": "outputs/报告.docx"})
        self.assertEqual(r2[0].state, "denied")

    # -- D5: a malformed agent manifest must not crash discovery ------------

    def test_noniterable_manifest_skills_degrades_not_crashes(self):
        # review D5: {"skills": 5} raised TypeError out of
        # _parse_agent_manifest and crashed every deliverable-shaped call.
        bad = Path(self.ws) / "agents" / "Bad"
        bad.mkdir(parents=True)
        (bad / "agent.json").write_text(
            '{"id":"Bad","skills":5}', encoding="utf-8"
        )
        self._bust()
        with self.assertLogs("dispatch_guard.qwenpaw", level="WARNING") as logs:
            r = run(self.mw, "write_file", {"file_path": "outputs/a.docx"})
        self.assertEqual(r[0].state, "denied")  # DocAgent still owns .docx
        self.assertTrue(any("non-list skills" in line for line in logs.output))

    # -- F17: guard-internal errors fail open, never wedge the agent --------

    def test_internal_error_fails_open_with_warning(self):
        with mock.patch.object(
            self.mw, "_route_hit", side_effect=RuntimeError("boom")
        ):
            with self.assertLogs("dispatch_guard.qwenpaw", level="ERROR") as logs:
                r = run(self.mw, "write_file", {"file_path": "outputs/a.docx"})
        self.assertNotEqual(r[0].state, "denied")
        texts = [getattr(b, "text", "") for b in r[0].content]
        self.assertTrue(any("内部异常" in text for text in texts))
        self.assertTrue(any("failing open" in line for line in logs.output))
        audit = Path(self.ws) / "logs" / "dispatch_guard.jsonl"
        actions = [
            json.loads(line)["action"]
            for line in audit.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn("warned", actions)

    # -- D2: a Mapping-shaped tool_call must not silently disarm the guard --

    def test_dict_tool_call_does_not_disarm_spawn_backstop(self):
        # review D2: getattr(tool_call, "name", "") read "" from a dict, so a
        # Mapping-shaped spawn call slipped the backstop entirely.
        call = {"name": "spawn_subagent", "input": {"task": "x"}}
        r = run(self.mw, "spawn_subagent", None, call=call)
        self.assertEqual(r[0].state, "denied")

    def test_unknown_tool_call_shape_is_audited_not_silent(self):
        # Unrecognizable shape: audited + fail-open pass, never silent.
        r = run(self.mw, "write_file", None, call=object())
        self.assertNotEqual(r[0].state, "denied")
        audit = Path(self.ws) / "logs" / "dispatch_guard.jsonl"
        records = [
            json.loads(line)
            for line in audit.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(
            any(
                rec["action"] == "warned" and rec["tool"] == "unknown"
                for rec in records
            )
        )
        # ...but when anything about it smells like spawn, refuse.
        r2 = run(self.mw, "x", None, call={"payload": "spawn_subagent --foo"})
        self.assertEqual(r2[0].state, "denied")

    # -- D3: non-string path values must not be str()'d into fake paths -----

    def test_list_valued_file_path_is_denied(self):
        # review D3: ["outputs/a.docx"] used to become the literal path
        # "['outputs/a.docx']" and pass untouched.
        r = run(self.mw, "write_file", {"file_path": ["outputs/a.docx"]})
        self.assertEqual(r[0].state, "denied")

    def test_dict_valued_file_path_is_denied(self):
        r = run(self.mw, "write_file", {"file_path": {"save": "outputs/a.docx"}})
        self.assertEqual(r[0].state, "denied")

    def test_non_deliverable_typed_value_still_passes(self):
        # The conservative escalation must not swallow basic operations.
        r = run(self.mw, "write_file", {"file_path": ["notes/ok.md"]})
        self.assertNotEqual(r[0].state, "denied")

    # -- D6/D7: shell write-target coverage (every variant, parametrized) ---

    _SHELL_ENFORCE_CFG = {
        "mode": "enforce",
        "shell_enforce": True,
        "ext_routes": {".docx": "DocAgent"},
        "routes": [{"match": ["outputs"], "agent": "DocAgent"}],
    }

    def test_shell_redirect_variants_are_seen(self):
        # review D6: >| (noclobber) and tee's long options were invisible —
        # the capture class excludes "|", and -\w+ cannot spell "--append".
        variants = [
            "echo x > outputs/r.docx",
            "echo x >> outputs/r.docx",
            "echo x >| outputs/r.docx",
            "echo x | tee outputs/r.docx",
            "echo x | tee -a outputs/r.docx",
            "echo x | tee --append outputs/r.docx",
        ]
        for cmd in variants:
            with self.subTest(cmd=cmd):
                self._write_config(dict(self._SHELL_ENFORCE_CFG))
                r = run(self.mw, "execute_shell_command", {"command": cmd})
                self.assertEqual(r[0].state, "denied")

    def test_shell_producer_families_are_seen(self):
        # review D7: downloader/archiver/compiler/in-place-editor outputs
        # were a whole class of invisible deliverable producers.
        variants = [
            "curl -o outputs/a.docx https://x/y",
            "curl -O https://x/y/a.docx",
            "wget -O outputs/a.docx https://x/y",
            "wget --output-document=outputs/a.docx https://x/y",
            "dd if=/tmp/z of=outputs/d.docx",
            "zip -r outputs/b.zip src/",
            "tar czf outputs/b.tgz src/",
            "gcc -o outputs/prog.exe main.c",
            "gcc -ooutputs/prog.exe main.c",
            "python gen.py --outdir outputs",
            "pandoc a.md -o outputs/r.docx",
            "soffice --headless --convert-to docx --outdir outputs x.odt",
            "sed -i s/x/y/ outputs/s.docx",
            "sed --in-place s/x/y/ outputs/s.docx",
            "perl -pi -e s/x/y/ outputs/p.docx",
        ]
        for cmd in variants:
            with self.subTest(cmd=cmd):
                self._write_config(dict(self._SHELL_ENFORCE_CFG))
                r = run(self.mw, "execute_shell_command", {"command": cmd})
                self.assertEqual(r[0].state, "denied")

    def test_shell_producer_family_negative_controls(self):
        # Reads and non-output shapes of the same tools must stay silent:
        # coverage is about writes, not about punishing inspection.
        variants = [
            "grep -o pat notes/x.md",
            "tar xf inputs/b.tgz",
            "unzip inputs/b.zip -d /tmp/x",
            "sed -n s/x/y/p notes/x.md",
            "ffmpeg -i clip.mov",
            "curl -s https://x/y",
        ]
        for cmd in variants:
            with self.subTest(cmd=cmd):
                self._write_config(dict(self._SHELL_ENFORCE_CFG))
                r = run(self.mw, "execute_shell_command", {"command": cmd})
                self.assertNotEqual(r[0].state, "denied")
                self.assertEqual(len(r[0].content), 1, f"no warn block for {cmd!r}")

    # -- D9: trailing-dot / trailing-space ext perturbation ------------------

    def test_perturbed_ext_still_routes(self):
        # review D9: "report.docx." / "report.docx " made splitext see "." or
        # ".docx " — the target stopped being a deliverable at all, and on a
        # pure ext_routes table the write escaped even without the dot game.
        self._write_config(
            {"mode": "enforce", "ext_routes": {".docx": "DocAgent"}, "routes": []}
        )
        for p in ("outputs/a.docx.", "outputs/a.docx ", "report.docx."):
            with self.subTest(path=p):
                r = run(self.mw, "write_file", {"file_path": p})
                self.assertEqual(r[0].state, "denied")

    # -- D11: a legit path key must not close the input-body scan ------------

    def test_nested_param_deliverable_gets_mention_warning(self):
        # review D11: file_path "notes/ok.md" short-circuited the whole
        # scan — a deliverable hidden in another argument was invisible.
        self._write_config(
            {"mode": "enforce", "ext_routes": {".docx": "DocAgent"}, "routes": []}
        )
        r = run(
            self.mw,
            "write_file",
            {"file_path": "notes/ok.md", "extra": {"save_as": "outputs/a.docx"}},
        )
        self.assertNotEqual(r[0].state, "denied", "mention tier never blocks")
        self.assertEqual(len(r[0].content), 2, "mention must warn")

    # -- D16: non-string deliverable_dirs members -----------------------------

    def test_nonstring_deliverable_dirs_members_dropped(self):
        # review D16: str() turned None/5 into "none/"/"5/" directories —
        # the config was silently mutated instead of degraded field-wise.
        self._write_config(
            {
                "mode": "enforce",
                "deliverable_dirs": [None, 5, "artifacts"],
                "ext_routes": {},
                "routes": [{"match": ["widget"], "agent": "DocAgent"}],
            }
        )
        r = run(self.mw, "write_file", {"file_path": "none/widget"})
        self.assertEqual(len(r[0].content), 1, "None must not invent a dir")
        r = run(self.mw, "write_file", {"file_path": "5/widget"})
        self.assertEqual(len(r[0].content), 1, "5 must not invent a dir")
        r = run(self.mw, "write_file", {"file_path": "artifacts/widget"})
        self.assertEqual(r[0].state, "denied", "the real entry still applies")

    # -- D10: spawn denial must not persist the task text verbatim -----------

    def test_spawn_denial_is_digest_not_verbatim(self):
        # review D10: the spawn backstop logged json.dumps(input) into the
        # audit trail — task text carries credentials and the log outlives
        # the call. Record now: sha256[:16] + length + redacted excerpt.
        secret = "sk-LIVE-TOKEN-9f2b"
        r = run(self.mw, "spawn_subagent", {"task": f"use {secret} now"})
        self.assertEqual(r[0].state, "denied")
        log = Path(self.ws) / "logs" / "dispatch_guard.jsonl"
        text = log.read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertIn("sha256:", text)
        self.assertIn("[redacted]", text)

    # -- D13: a broken reload keeps the last known-good table ---------------

    def test_parse_failure_keeps_last_known_good_table(self):
        # review D13: a half-written routes.json used to fail open to "off"
        # (silent disarmament) even though the cache held a good table.
        self._write_config(
            {"mode": "enforce", "ext_routes": {".docx": "DocAgent"}, "routes": []}
        )
        r = run(self.mw, "write_file", {"file_path": "outputs/a.docx"})
        self.assertEqual(r[0].state, "denied")
        self.routes_path.write_text("{ half-written", encoding="utf-8")
        dg._CONFIG_CACHE["mtime"] = 0.0  # force the next call to re-read
        r = run(self.mw, "write_file", {"file_path": "outputs/a.docx"})
        self.assertEqual(r[0].state, "denied", "last-known-good stays in force")
        # cold cache + broken file: nothing to keep, still fails open to off.
        dg._CONFIG_CACHE.update({"data": None, "mtime": 0.0, "source": None})
        r = run(self.mw, "write_file", {"file_path": "outputs/a.docx"})
        self.assertNotEqual(r[0].state, "denied")

    # -- D15: a missing routes key is legal, not a type error ---------------

    def test_missing_routes_key_produces_no_wrong_type_warning(self):
        # review D15: minimal {"mode":…,"ext_routes":…} tables spammed the
        # "'routes' must be a JSON array" warning on every load — missing
        # key and wrong type are different facts.
        def _warnings_for(cfg_obj):
            self.routes_path.write_text(
                json.dumps(cfg_obj, ensure_ascii=False), encoding="utf-8"
            )
            dg._CONFIG_CACHE.update({"data": None, "mtime": 0.0, "source": None})
            messages: list[str] = []

            class _Capture(logging.Handler):
                def emit(self, record):
                    messages.append(record.getMessage())

            h = _Capture()
            dg.logger.addHandler(h)
            try:
                dg._load_config()
            finally:
                dg.logger.removeHandler(h)
            return [m for m in messages if "must be a JSON array" in m]

        self.assertEqual(
            _warnings_for({"mode": "enforce", "ext_routes": {".docx": "DocAgent"}}),
            [],
            "absent routes key must stay quiet",
        )
        self.assertEqual(
            len(_warnings_for({"mode": "enforce", "routes": None})),
            1,
            "explicit null routes is still a type error worth flagging",
        )


if __name__ == "__main__":
    unittest.main()
