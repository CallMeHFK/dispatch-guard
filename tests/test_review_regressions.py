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

    def test_shell_joined_and_long_option_outputs_are_seen(self):
        # B6: three shapes the D7 patterns still missed.
        #   - a short output flag with its value joined: curl -oFILE. The
        #     generic flag pattern required "=" or whitespace after the flag.
        #   - the same joined form on a compiler whose name ends in "+": the
        #     compiler pattern's trailing \b never matched after "g++" (two
        #     non-word characters make no boundary), so only gcc/cc/clang were
        #     covered and g++ slipped through.
        #   - tar's long-option spelling: --create --file=ARCHIVE, which the
        #     short-bundle pattern ([a-zA-Z]*c[a-zA-Z]*f) cannot spell.
        variants = [
            "curl -ooutputs/a.docx https://x/y",
            "curl -Ooutputs/a.docx https://x/y",
            "wget -Ooutputs/a.docx https://x/y",
            "g++ -ooutputs/prog.exe main.cpp",
            "clang++ -ooutputs/prog.exe main.cpp",
            "tar --create --file=outputs/b.tgz src/",
            "tar --create --file outputs/b.tgz src/",
            "tar --file=outputs/b.tgz --create src/",
        ]
        for cmd in variants:
            with self.subTest(cmd=cmd):
                self._write_config(dict(self._SHELL_ENFORCE_CFG))
                r = run(self.mw, "execute_shell_command", {"command": cmd})
                self.assertEqual(r[0].state, "denied")

    def test_shell_non_create_and_or_operator_are_not_denied(self):
        # B6 negative controls: listing, extracting and find's -o operator
        # are not writes, and a bare non-deliverable -o target is not a
        # deliverable. A mention-level warn is acceptable here — a deny is
        # not, and neither is a new explicit-target match.
        variants = [
            "tar tf outputs/b.tgz",
            "tar --list --file=outputs/b.tgz",
            "tar xf outputs/b.tgz -C /tmp",
            "tar --extract --file=outputs/b.tgz",
            "find . -name '*.docx' -o -name '*.pdf'",
            "ls outputs",
            "cat outputs/a.docx",
            "curl -o /tmp/plain https://x/y",
            "g++ -c main.cpp",
        ]
        for cmd in variants:
            with self.subTest(cmd=cmd):
                self._write_config(dict(self._SHELL_ENFORCE_CFG))
                r = run(self.mw, "execute_shell_command", {"command": cmd})
                self.assertNotEqual(r[0].state, "denied")

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

    # -- D1 shell arm, narrowed by B1: the guard's own table, by real path --

    _GUARD_TABLE_CFG = {
        "mode": "enforce",
        "ext_routes": {".docx": "DocAgent"},
        "routes": [],
    }

    def _guard_table_cfg(self, **over):
        cfg = dict(self._GUARD_TABLE_CFG)
        cfg.update(over)
        self._write_config(cfg)
        return str(dg.PLUGIN_DIR / "routes.json")

    def test_shell_write_to_guard_table_by_real_path_is_denied(self):
        # review D1-shell: the write-tool branch denied explicit guard-table
        # targets, but `sed -i`/`tee`/redirects past routes.json sailed
        # through — the one bypass that un-does every other rule. What the
        # arm keys on is the guard's own real path, not the basename: a
        # relative spelling cannot reach a plugin directory outside the
        # workspace, so a bare "routes.json" is somebody else's file.
        table = self._guard_table_cfg()
        variants = [
            f"echo '{{\"mode\":\"off\"}}' > {table}",
            f"sed -i s/enforce/off/ {table}",
            f"cat /etc/hosts | tee {table}",
            f"python -c \"open(r'{table}','w').write(x)\"",
            f"rm -f {table}",
            f"mv {table} /tmp/gone.json",
            f"truncate -s 0 {table}",
            f"chmod 000 {table}",
        ]
        for cmd in variants:
            with self.subTest(cmd=cmd):
                r = run(self.mw, "execute_shell_command", {"command": cmd})
                self.assertEqual(r[0].state, "denied")

    def test_shell_write_to_guard_table_ignores_shell_enforce(self):
        # Meta-integrity is an exact-path match, not a shell heuristic, so it
        # neither needs nor respects the shell_enforce opt-in.
        for shell_enforce in (False, True):
            with self.subTest(shell_enforce=shell_enforce):
                table = self._guard_table_cfg(shell_enforce=shell_enforce)
                r = run(
                    self.mw,
                    "execute_shell_command",
                    {"command": f"echo x > {table}"},
                )
                self.assertEqual(r[0].state, "denied")

    def test_shell_write_to_guard_table_denied_under_mode_off(self):
        # off disarms routing, not meta-integrity — the same ruling as the
        # write-tool branch. A disarmed guard still refuses the one write
        # that keeps it disarmed.
        table = self._guard_table_cfg(mode="off")
        r = run(
            self.mw,
            "execute_shell_command",
            {"command": f"echo '{{\"mode\":\"off\"}}' > {table}"},
        )
        self.assertEqual(r[0].state, "denied")

    def test_shell_read_of_guard_table_warns_but_never_denies(self):
        # DESIGN.md's mention tier: naming the table earns a warning and
        # nothing more. Reading it is not an attack, and read_file on the
        # same path is not guarded at all — denying only the shell spelling
        # would be friction with no boundary behind it.
        table = self._guard_table_cfg()
        for cmd in (f"cat {table}", f"grep -n mode {table}", f"jq .mode {table}"):
            with self.subTest(cmd=cmd):
                r = run(self.mw, "execute_shell_command", {"command": cmd})
                self.assertNotEqual(r[0].state, "denied")
                self.assertEqual(len(r[0].content), 2, "warn block must be appended")

    def test_shell_commands_on_a_project_own_routes_json_are_untouched(self):
        # B1: the arm matched the bare basename anywhere in the command text,
        # so under enforce — with shell_enforce OFF — every project that
        # happens to own a routes.json (Express, Angular, this repository)
        # could not read, grep, copy or clean up its own file. All of these
        # were denied before the fix.
        self._guard_table_cfg()
        variants = [
            "cat src/routes.json",
            "cat routes.json",
            "grep -n docx routes.json",
            "python -c \"print(open('routes.json').read())\"",
            "rm -rf /tmp/x  # routes.json",
            "cp routes.example.json routes.json",
            "rm -f routes.json",
            "cp backup/routes.draft.json ./routes.json",
        ]
        for cmd in variants:
            with self.subTest(cmd=cmd):
                r = run(self.mw, "execute_shell_command", {"command": cmd})
                self.assertNotEqual(r[0].state, "denied")
                self.assertEqual(len(r[0].content), 1, f"silent for {cmd!r}")

    def test_guard_table_mention_warns_not_denies_outside_enforce(self):
        cfg = dict(self._GUARD_TABLE_CFG)
        cfg["mode"] = "warn"
        self._write_config(cfg)
        r = run(
            self.mw,
            "execute_shell_command",
            {"command": f"cat {dg.PLUGIN_DIR / 'routes.json'}"},
        )
        self.assertNotEqual(r[0].state, "denied", "mention level outside enforce")
        self.assertEqual(len(r[0].content), 2, "warn block must be appended")

    def test_guard_table_mention_lookalikes_stay_innocent(self):
        # routes.example.json is the shipped sanitized table; a boundary-
        # anchored regex must not fire on it or on "myroutes.json".
        self._write_config(dict(self._GUARD_TABLE_CFG))
        for cmd in (
            "cp routes.example.json myroutes.json",
            "cat myroutes.json",
            "grep -r dispatch routes.example.json",
        ):
            with self.subTest(cmd=cmd):
                r = run(self.mw, "execute_shell_command", {"command": cmd})
                self.assertNotEqual(r[0].state, "denied")
                self.assertEqual(len(r[0].content), 1, f"silent for {cmd!r}")

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

    # -- B2 (Phase 3): the D2 unknown-shape path logged str(tool_call)
    #    verbatim — D10's redaction only covered the spawn branch. Both the
    #    deny and the warn leg must persist the digest form, never raw text.

    def test_unknown_shape_denial_does_not_persist_secret(self):
        # Each payload stays under the 80-char excerpt cap so the assertion
        # tests redaction itself, not accidental truncation.
        cases = {
            "sk-LIVE-TOKEN-9f2b": "spawn a worker, auth sk-LIVE-TOKEN-9f2b",
            "Correct-Horse-Battery-Staple": (
                "spawn a worker, password is Correct-Horse-Battery-Staple"
            ),
            "AKIAIOSFODNN7EXAMPLE": "spawn a worker, key AKIAIOSFODNN7EXAMPLE",
            "token=hunter2example": "spawn a worker, token=hunter2example",
            "Bearer Zxy9abc123Q": "spawn a worker, header Bearer Zxy9abc123Q",
        }
        log = Path(self.ws) / "logs" / "dispatch_guard.jsonl"
        for secret, payload in cases.items():
            with self.subTest(secret=secret):
                r = run(self.mw, "x", None, call={"payload": payload})
                self.assertEqual(r[0].state, "denied")
                text = log.read_text(encoding="utf-8")
                self.assertNotIn(secret, text)
        self.assertIn("sha256:", log.read_text(encoding="utf-8"))

    def test_unknown_shape_warn_does_not_persist_secret(self):
        secret = "sk-LIVE-TOKEN-9f2b"
        # No spawn smell -> the warned/pass leg of the unknown-shape branch.
        r = run(self.mw, "x", None, call={"payload": f"odd shape carrying {secret}"})
        self.assertNotEqual(r[0].state, "denied")
        log = Path(self.ws) / "logs" / "dispatch_guard.jsonl"
        text = log.read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertIn("sha256:", text)

    # -- B5 (Phase 3): canonicalisation ran strip() once and rstrip(".") once,
    #    in that order. A spelling that ends in a dot defeats strip(), so
    #    "report.docx ." came out as "report.docx " — an extension with a
    #    trailing space, which matched no route and left no audit record.

    def test_canonical_target_is_a_fixed_point(self):
        cases = {
            "report.docx": "report.docx",
            "report.docx.": "report.docx",
            "report.docx ": "report.docx",
            "report.docx .": "report.docx",
            "report.docx. ": "report.docx",
            "a.docx . .": "a.docx",
            "outputs/a.docx .": "outputs/a.docx",
            "report.docx\xa0": "report.docx",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(dg._canonical_target(raw), want)

    def test_trailing_space_then_dot_is_not_a_bypass(self):
        log = Path(self.ws) / "logs" / "dispatch_guard.jsonl"
        for raw in ("report.docx .", "outputs/a.docx .", "a.docx . ."):
            with self.subTest(target=raw):
                if log.exists():
                    log.unlink()
                r = run(self.mw, "write_file", {"file_path": raw, "content": "x"})
                self.assertEqual(r[0].state, "denied")
                # The failure mode this guards is silence, not just a pass:
                # a target that stops looking deliverable produces no record
                # at all, so the operator never learns the write happened.
                self.assertTrue(log.exists(), "denial left no audit record")
                self.assertIn("denied", log.read_text(encoding="utf-8"))

    def test_whitelist_uses_the_same_canonical_yardstick(self):
        # D12 made the whitelist case-insensitive so one spelling of a path
        # gets one answer. Trailing dots and spaces are the same class of
        # spelling accident: _is_deliverable canonicalises, so _whitelisted
        # must too, or the two halves of the decision disagree about what
        # string they are judging.
        for raw in ("MEMORY.md", "MEMORY.md.", "MEMORY.md ", "MEMORY.md ."):
            with self.subTest(target=raw):
                self.assertTrue(dg.DispatchGuardMiddleware._whitelisted(raw))
        for raw in ("outputs/a.docx", "outputs/a.docx ."):
            with self.subTest(target=raw):
                self.assertFalse(dg.DispatchGuardMiddleware._whitelisted(raw))

    # -- B3 (Phase 3): meta-integrity only covered a plain string path in a
    #    mode other than off. Four shapes walked around it, and the whitelist
    #    exclusion was unreachable for any table under a whitelisted prefix.

    def test_guard_table_write_denied_under_mode_off(self):
        # off disarms routing, not meta-integrity: the one write that must
        # never succeed is the one that rewrites the guard's own authority.
        self._write_config({"mode": "off", "ext_routes": {".docx": "DocAgent"}})
        target = str(dg.PLUGIN_DIR / "routes.json")
        r = run(self.mw, "write_file", {"file_path": target, "content": "{}"})
        self.assertEqual(r[0].state, "denied")
        self.assertIn("元完整性", r[0].content[0].text)

    def test_guard_table_write_denied_for_non_string_path_values(self):
        # The check was gated on target_is_path, which the D3 shapes clear.
        target = str(dg.PLUGIN_DIR / "routes.json")
        shapes = {
            "list": [target],
            "dict": {"save": target},
            "nested": {"a": {"b": [target]}},
        }
        for label, value in shapes.items():
            with self.subTest(shape=label):
                r = run(self.mw, "write_file", {"file_path": value, "content": "{}"})
                self.assertEqual(r[0].state, "denied")

    def test_guard_table_write_denied_case_insensitively(self):
        # A case-insensitive host resolves ROUTES.JSON to the same file as
        # routes.json, so the yardstick must be case-insensitive too — the
        # same call D12 made for the whitelist.
        target = str(dg.PLUGIN_DIR / "ROUTES.JSON")
        r = run(self.mw, "write_file", {"file_path": target, "content": "{}"})
        self.assertEqual(r[0].state, "denied")

    def test_guard_table_write_denied_with_trailing_dot(self):
        for spelling in ("routes.json.", "routes.json ", "routes.json ."):
            with self.subTest(spelling=spelling):
                target = str(dg.PLUGIN_DIR / spelling)
                r = run(self.mw, "write_file", {"file_path": target, "content": "{}"})
                self.assertEqual(r[0].state, "denied")

    def test_guard_table_basenames_are_never_whitelisted(self):
        # The exclusion sat below two early returns, so a table parked under
        # a whitelisted prefix returned True and never reached it.
        for rel in ("tmp/routes.json", "notes/routes.draft.json", "routes.json"):
            with self.subTest(rel=rel):
                self.assertFalse(dg.DispatchGuardMiddleware._whitelisted(rel))

    def test_guard_table_tilde_spelling_is_a_mutation_not_a_mention(self):
        # B7: _guard_table_paths publishes the ~-compressed spelling because
        # that is what an operator or an agent is likeliest to type, so the
        # naming tier matched it — but the mutation tier resolves through
        # _is_guard_config, which joined "~" onto the workspace instead of
        # expanding it. A redirect, `sed -i` or `tee` aimed at the ~ form
        # therefore warned instead of being refused, while the identical
        # absolute spelling was refused: the documented "mutating one is
        # refused in every mode" was false for the spelling the docs invite.
        fake = Path.home() / ".dg-tilde-probe" / "plugins" / "dispatch-guard"
        rel = ".dg-tilde-probe/plugins/dispatch-guard/routes.json"
        saved = dg.PLUGIN_DIR
        dg.PLUGIN_DIR = fake
        try:
            self.assertTrue(self.mw._is_guard_config(f"~/{rel}"))
            self.assertTrue(self.mw._is_guard_config(f"~/{rel}."))
            for cmd in (
                f"echo '{{}}' > ~/{rel}",
                f"sed -i s/enforce/off/ ~/{rel}",
                f"printf x | tee ~/{rel}",
                f"cp /tmp/evil.json ~/{rel}",
            ):
                with self.subTest(cmd=cmd):
                    named, is_write = self.mw._guard_table_shell_hit(cmd)
                    self.assertIsNotNone(named)
                    self.assertTrue(is_write, "~ spelling must count as a mutation")
            # The absolute spelling already worked; do not regress it.
            _, is_write = self.mw._guard_table_shell_hit(
                f"echo '{{}}' > {fake / 'routes.json'}"
            )
            self.assertTrue(is_write)
        finally:
            dg.PLUGIN_DIR = saved


if __name__ == "__main__":
    unittest.main()
