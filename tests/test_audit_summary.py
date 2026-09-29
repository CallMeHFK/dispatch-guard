#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the audit-log summarizer that backs the warn->enforce decision."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packaging"))

import audit_summary  # noqa: E402

RECORDS = [
    {"ts": 1, "action": "needs_config", "mode": "unconfigured", "target": "outputs/a.docx"},
    {"ts": 2, "action": "warned", "mode": "warn", "target": "outputs/a.docx"},
    {
        "ts": 3,
        "action": "denied",
        "mode": "enforce",
        "target": "outputs/a.docx",
        "message": "该产出属于 DocAgent 域（命中 ext .docx），请用 submit_to_agent 派发给 DocAgent；…",
    },
    {
        "ts": 4,
        "action": "denied",
        "mode": "enforce",
        "target": "outputs/a.docx",
        "message": "该产出属于 DocAgent 域（命中 ext .docx），请用 submit_to_agent 派发给 DocAgent；…",
    },
    {"ts": 5, "action": "no_owner", "mode": "enforce", "target": "outputs/poster.svg"},
    {"ts": 6, "action": "no_owner", "mode": "enforce", "target": "outputs/bundle.zip"},
]


class AuditSummaryTest(unittest.TestCase):
    def test_counts_and_attributions(self):
        s = audit_summary.summarize(RECORDS)
        self.assertEqual(s["events"], 6)
        self.assertEqual(s["actions"], {"needs_config": 1, "warned": 1, "denied": 2, "no_owner": 2})
        self.assertEqual(s["modes"], {"unconfigured": 1, "warn": 1, "enforce": 4})
        self.assertEqual(s["denied_by_agent"], {"DocAgent": 2})

    def test_no_owner_targets_are_reported(self):
        s = audit_summary.summarize(RECORDS)
        self.assertEqual(set(s["no_owner_targets"]), {"outputs/poster.svg", "outputs/bundle.zip"})

    def test_report_mentions_specialist_gaps(self):
        report = audit_summary.format_report(audit_summary.summarize(RECORDS))
        self.assertIn("no live owner", report)
        self.assertIn("outputs/poster.svg", report)
        self.assertIn("DocAgent", report)


if __name__ == "__main__":
    unittest.main()
