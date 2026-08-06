"""The retention audit, in report-only mode.

Most of these protect a decision rather than a mechanism: what counts as
evidence, when silence is allowed to mean anything, and what the sweep is not
permitted to do.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from project_memory_mcp.audit import (
    VERDICT_ARCHIVE,
    VERDICT_HOLD,
    VERDICT_PROMOTE,
    AuditPolicy,
    TierGate,
    format_report,
    last_run,
    run_audit,
)
from project_memory_mcp.sqlite_store import SqliteMemoryStore

CACHE = "Session cache invalidation races the auth refresh under load."
SHADER = "Shader compilation stalls on a cold start on the build farm."


def memory(memory_id, description, labels, related=None):
    return {
        "schema_version": 1, "id": memory_id, "status": "active", "description": description,
        "tags": [], "labels": labels,
        "scope": {"project": "p", "area": "a", "files": [], "applies_to": []},
        "triggers": ["trigger for " + memory_id], "remembered_facts": [description],
        "solution_pattern": [], "pitfalls": [],
        "evidence": {"created_from_task": "t", "last_validated": "2026-01-01"},
        "relationships": {
            "related": [{"id": o, "reason": "They share a subsystem."} for o in (related or [])],
            "supersedes": [], "superseded_by": [],
        },
    }


def verdicts(report):
    return {f.slug: f.verdict for f in report.findings}


class AuditCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "memory.db"
        self.store = SqliteMemoryStore(self.db, "demo")
        self.addCleanup(self.store.close)
        for label in ("area:x", "area:z"):
            self.store.add_label(label, "description for " + label)
        # A gate that is reachable in a test, with the same shape as the real one.
        self.policy = AuditPolicy(gates=(TierGate(tier=1, min_queries=5, min_days=0),))

    def age(self, slug, days=0, queries=0):
        """Pretend a memory has sat in its tier for a while."""
        when = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.store.connection.execute(
            "UPDATE memories SET tier_since=?, tier_since_query=? WHERE project_id='demo' AND slug=?",
            (when, queries, slug))
        self.store.connection.commit()

    def serve(self, count, query="something nobody stored"):
        for _ in range(count):
            self.store.recall(query, limit=1, full_count=0)


class ExposureTests(AuditCase):
    def test_a_memory_is_not_judged_before_it_has_had_a_chance(self):
        # `surfaced` is decided by the ranker: something that keeps landing
        # eleventh when recall returns ten is never seen, and judging it on that
        # would make a ranking error permanent.
        self.store.create_memory(memory("quiet-note", CACHE, ["area:x"]))
        report = run_audit(self.store, self.policy)
        self.assertEqual(VERDICT_HOLD, verdicts(report)["quiet-note"])
        self.assertIn("exposure", report.findings[0].reason)

    def test_silence_becomes_evidence_once_the_project_has_served_queries(self):
        self.store.create_memory(memory("quiet-note", CACHE, ["area:x"]))
        self.age("quiet-note", queries=0)
        self.serve(6)
        report = run_audit(self.store, self.policy)
        self.assertEqual(VERDICT_ARCHIVE, verdicts(report)["quiet-note"])

    def test_a_dormant_project_ages_nothing(self):
        # Six weeks of nobody working is not six weeks of a memory being useless.
        self.store.create_memory(memory("quiet-note", CACHE, ["area:x"]))
        self.age("quiet-note", days=400, queries=0)
        report = run_audit(self.store, self.policy)
        self.assertEqual(VERDICT_HOLD, verdicts(report)["quiet-note"])

    def test_real_time_must_pass_as_well_as_exposure(self):
        # A hundred queries in one afternoon is one task, not a month of proof.
        policy = AuditPolicy(gates=(TierGate(tier=1, min_queries=5, min_days=30),))
        self.store.create_memory(memory("quiet-note", CACHE, ["area:x"]))
        self.age("quiet-note", days=1, queries=0)
        self.serve(6)
        report = run_audit(self.store, policy)
        self.assertEqual(VERDICT_HOLD, verdicts(report)["quiet-note"])
        self.assertIn("days in tier", report.findings[0].reason)

    def test_exposure_counts_from_when_the_memory_entered_its_tier(self):
        self.serve(20)  # queries served before this memory existed
        self.store.create_memory(memory("newcomer", CACHE, ["area:x"]))
        report = run_audit(self.store, self.policy)
        self.assertEqual(VERDICT_HOLD, verdicts(report)["newcomer"])


class EvidenceTests(AuditCase):
    def setUp(self):
        super().setUp()
        self.store.create_memory(memory("subject", CACHE, ["area:x"]))
        self.age("subject", queries=0)

    def test_a_direct_match_earns_promotion(self):
        self.store.recall("cache invalidation")
        self.serve(6)
        report = run_audit(self.store, self.policy)
        self.assertEqual(VERDICT_PROMOTE, verdicts(report)["subject"])
        self.assertIn("matched a query directly", report.findings[0].reason)

    def test_being_reported_as_applied_earns_promotion(self):
        self.store.record_use(["subject"])
        self.serve(6)
        report = run_audit(self.store, self.policy)
        self.assertEqual(VERDICT_PROMOTE, verdicts(report)["subject"])

    def test_incoming_links_carry_a_memory_that_never_matched(self):
        # Something several memories point at is load-bearing even if it rarely
        # wins a query on its own.
        for name in ("ref-one", "ref-two"):
            self.store.create_memory(memory(name, SHADER + " " + name, ["area:z"], related=["subject"]))
            self.age(name, queries=0)
        self.serve(6)
        report = run_audit(self.store, self.policy)
        self.assertEqual(VERDICT_PROMOTE, verdicts(report)["subject"])
        self.assertIn("link to it", [f for f in report.findings if f.slug == "subject"][0].reason)

    def test_graph_surfacing_alone_does_not_earn_promotion(self):
        # The neighbour rides along in the results; that is not its own evidence.
        self.store.create_memory(memory("popular", SHADER, ["area:z"], related=["subject"]))
        self.age("popular", queries=0)
        self.store.recall("shader compilation cold start")
        self.serve(6)
        finding = [f for f in run_audit(self.store, self.policy).findings if f.slug == "subject"][0]
        self.assertGreater(finding.evidence["surfaced"], 0)
        self.assertEqual(0, finding.evidence["surfaced_direct"])

    def test_the_thresholds_are_policy_not_constants(self):
        self.store.recall("cache invalidation")
        self.serve(6)
        strict = AuditPolicy(gates=self.policy.gates, min_surfaced_direct=5,
                             min_applied=99, min_spread_days=99, min_degree=99)
        self.assertEqual(VERDICT_ARCHIVE, verdicts(run_audit(self.store, strict))["subject"])


class SafetyTests(AuditCase):
    def test_the_sweep_never_deletes_and_never_changes_status(self):
        self.store.create_memory(memory("quiet-note", CACHE, ["area:x"]))
        self.age("quiet-note", queries=0)
        self.serve(6)
        run_audit(self.store, self.policy)
        self.assertEqual("active", self.store.get_memory("quiet-note")["status"])
        self.assertEqual(1, self.store.count())

    def test_tiers_are_not_moved_in_report_only_mode(self):
        self.store.create_memory(memory("subject", CACHE, ["area:x"]))
        self.age("subject", queries=0)
        self.store.recall("cache invalidation")
        self.serve(6)
        report = run_audit(self.store, self.policy)
        self.assertEqual(VERDICT_PROMOTE, verdicts(report)["subject"])
        tier = self.store.connection.execute(
            "SELECT tier FROM memories WHERE slug='subject'").fetchone()["tier"]
        self.assertEqual(1, tier)

    def test_applying_is_refused_rather_than_half_implemented(self):
        with self.assertRaises(NotImplementedError):
            run_audit(self.store, self.policy, apply=True)

    def test_one_run_may_only_act_on_a_capped_number(self):
        for i in range(10):
            self.store.create_memory(memory(f"note-{i}", f"{CACHE} number {i}", ["area:x"]))
            self.age(f"note-{i}", queries=0)
        self.serve(6)
        policy = AuditPolicy(gates=self.policy.gates, max_actions_per_run=3, max_action_fraction=1.0)
        report = run_audit(self.store, policy)
        self.assertEqual(3, report.archived)
        self.assertEqual(7, report.capped)
        held = [f for f in report.findings if f.verdict == VERDICT_HOLD]
        self.assertTrue(all("cap" in f.reason for f in held))

    def test_the_run_and_its_findings_are_recorded(self):
        self.store.create_memory(memory("quiet-note", CACHE, ["area:x"]))
        self.age("quiet-note", queries=0)
        self.serve(6)
        report = run_audit(self.store, self.policy)
        stored = last_run(self.store)
        self.assertEqual(report.run_id, stored["run"]["id"])
        self.assertEqual(0, stored["run"]["applied"])
        self.assertEqual(["quiet-note"], [f["id"] for f in stored["findings"]])
        self.assertEqual(VERDICT_ARCHIVE, stored["findings"][0]["verdict"])
        self.assertIn("exposure_queries", stored["findings"][0]["evidence"])

    def test_the_policy_used_is_recorded_with_the_run(self):
        # A verdict is only readable if you know what thresholds produced it.
        self.serve(6)
        run_audit(self.store, self.policy)
        policy = json.loads(last_run(self.store)["run"]["policy"])
        self.assertEqual(5, policy["gates"][0]["min_queries"])

    def test_rerunning_produces_the_same_verdicts(self):
        self.store.create_memory(memory("quiet-note", CACHE, ["area:x"]))
        self.age("quiet-note", queries=0)
        self.serve(6)
        first = verdicts(run_audit(self.store, self.policy))
        self.assertEqual(first, verdicts(run_audit(self.store, self.policy)))

    def test_a_run_can_be_computed_without_being_stored(self):
        self.serve(6)
        run_audit(self.store, self.policy, record=False)
        self.assertIsNone(last_run(self.store)["run"])


class ReportingTests(AuditCase):
    def test_the_ui_reading_the_store_does_not_change_its_metrics(self):
        # Reviewing an audit must not alter the data the audit is about.
        self.store.create_memory(memory("subject", CACHE, ["area:x"]))
        self.store.recall("cache invalidation", record=False)
        before = self.store.connection.execute(
            "SELECT queries FROM projects WHERE id='demo'").fetchone()["queries"]
        self.assertEqual(0, before)
        self.assertEqual({}, self.store.load_usage()["memories"])

    def test_the_report_says_what_it_would_do_and_why(self):
        self.store.create_memory(memory("quiet-note", CACHE, ["area:x"]))
        self.age("quiet-note", queries=0)
        self.serve(6)
        text = format_report(run_audit(self.store, self.policy))
        self.assertIn("report only, nothing changed", text)
        self.assertIn("quiet-note", text)
        self.assertIn("no direct match", text)

    def test_an_empty_store_reports_cleanly(self):
        report = run_audit(self.store, self.policy)
        self.assertEqual(0, report.examined)
        self.assertIn("0 memories", format_report(report))


if __name__ == "__main__":
    unittest.main()
