"""The retention audit: judging, acting, and merging.

Most of these protect a decision rather than a mechanism: what counts as
evidence, when silence is allowed to mean anything, what the sweep is not
permitted to do, and which judgments it is not allowed to make alone.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from project_memory_mcp.audit import (
    VERDICT_ARCHIVE,
    VERDICT_DELETE,
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

    def test_nothing_is_deleted_for_being_quiet(self):
        # The strongest rule in the design: silence is absence of evidence, and
        # deletion acts on evidence.
        self.store.create_memory(memory("quiet-note", CACHE, ["area:x"]))
        self.age("quiet-note", queries=0)
        self.serve(6)
        report = run_audit(self.store, self.policy, apply=True)
        self.assertEqual(VERDICT_ARCHIVE, verdicts(report)["quiet-note"])
        self.assertEqual(1, self.store.count())
        self.assertEqual(CACHE, self.store.get_memory("quiet-note")["description"])

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


class ApplyTests(AuditCase):
    def test_archiving_removes_a_memory_from_the_ranked_pool(self):
        self.store.create_memory(memory("quiet-note", CACHE, ["area:x"]))
        self.age("quiet-note", queries=0)
        self.serve(6)
        # record=False matters: a recall that counts would hand this memory the
        # direct match that saves it from the very verdict under test.
        self.assertTrue(self.store.recall("cache invalidation", record=False)["memories"])

        run_audit(self.store, self.policy, apply=True)

        self.assertEqual([], self.store.recall("cache invalidation", record=False)["memories"])
        self.assertEqual([], self.store.recall(order="recent", limit=10, record=False)["memories"])
        self.assertEqual([], self.store.search_memories()["memories"])

    def test_an_archived_memory_is_still_readable_and_restorable(self):
        # Archiving has to be recoverable, or acting on thin evidence costs a
        # permanent mistake instead of a reversible one.
        self.store.create_memory(memory("quiet-note", CACHE, ["area:x"]))
        self.age("quiet-note", queries=0)
        self.serve(6)
        run_audit(self.store, self.policy, apply=True)

        self.assertEqual(CACHE, self.store.get_memory("quiet-note")["description"])
        self.assertEqual(["quiet-note"], [m["id"] for m in self.store.archived()["memories"]])

        self.store.archive_memory("quiet-note", archived=False)
        self.assertTrue(self.store.recall("cache invalidation", record=False)["memories"])

    def test_promotion_moves_the_tier_and_restarts_its_clock(self):
        self.store.create_memory(memory("subject", CACHE, ["area:x"]))
        self.age("subject", queries=0)
        self.store.recall("cache invalidation")
        self.serve(6)
        run_audit(self.store, self.policy, apply=True)

        row = self.store.connection.execute(
            "SELECT tier, tier_since_query FROM memories WHERE slug='subject'").fetchone()
        self.assertEqual(2, row["tier"])
        self.assertGreater(row["tier_since_query"], 0)

    def test_a_promoted_memory_is_not_reviewed_again_immediately(self):
        # Each tier is a longer reprieve; that only works if the clock restarts.
        policy = AuditPolicy(gates=(TierGate(tier=1, min_queries=5, min_days=0),
                                    TierGate(tier=2, min_queries=500, min_days=0)))
        self.store.create_memory(memory("subject", CACHE, ["area:x"]))
        self.age("subject", queries=0)
        self.store.recall("cache invalidation")
        self.serve(6)
        run_audit(self.store, policy, apply=True)

        second = run_audit(self.store, policy, apply=True)
        self.assertEqual(VERDICT_HOLD, verdicts(second)["subject"])

    def test_an_archived_memory_is_not_judged_again(self):
        self.store.create_memory(memory("quiet-note", CACHE, ["area:x"]))
        self.age("quiet-note", queries=0)
        self.serve(6)
        run_audit(self.store, self.policy, apply=True)

        again = run_audit(self.store, self.policy, apply=True)
        self.assertEqual(VERDICT_HOLD, verdicts(again)["quiet-note"])
        self.assertEqual("already archived", again.findings[0].reason)

    def test_applying_twice_is_a_no_op_the_second_time(self):
        for i in range(4):
            self.store.create_memory(memory(f"note-{i}", f"{CACHE} number {i}", ["area:x"]))
            self.age(f"note-{i}", queries=0)
        self.serve(6)
        # The default cap is a fraction of the store, which rounds to 1 here;
        # this test is about idempotence, not about the cap.
        policy = AuditPolicy(gates=self.policy.gates, max_action_fraction=1.0)
        first = run_audit(self.store, policy, apply=True)
        second = run_audit(self.store, policy, apply=True)
        self.assertGreater(first.archived, 0)
        self.assertEqual(0, second.archived)
        self.assertEqual(0, second.promoted)

    def test_the_run_is_recorded_as_applied(self):
        self.store.create_memory(memory("quiet-note", CACHE, ["area:x"]))
        self.age("quiet-note", queries=0)
        self.serve(6)
        run_audit(self.store, self.policy, apply=True)
        self.assertEqual(1, last_run(self.store)["run"]["applied"])

    def test_archived_memories_do_not_consume_candidate_slots(self):
        # Filtering at the source rather than in the results is what stops an
        # archived memory taking a place a live one could have used.
        for i in range(3):
            self.store.create_memory(memory(f"cache-{i}", f"{CACHE} variant {i}", ["area:x"]))
            self.age(f"cache-{i}", queries=0)
        self.store.archive_memory("cache-0")
        self.store.archive_memory("cache-1")
        found = self.store.recall("cache invalidation", limit=3, record=False)["memories"]
        self.assertEqual(["cache-2"], [m["id"] for m in found])


class DeletionTests(AuditCase):
    def setUp(self):
        super().setUp()
        self.policy = AuditPolicy(gates=(TierGate(tier=1, min_queries=5, min_days=0),),
                                  delete_superseded=True)
        self.store.create_memory(memory("old-way", "Build the project with make release.", ["area:x"]))
        replacement = memory("new-way", "Build the project with ninja release instead.", ["area:x"])
        replacement["relationships"]["supersedes"] = ["old-way"]
        self.store.create_memory(replacement)

    def test_a_superseded_memory_with_a_live_successor_is_deletable(self):
        report = run_audit(self.store, self.policy)
        self.assertEqual(VERDICT_DELETE, verdicts(report)["old-way"])
        self.assertIn("still active", [f for f in report.findings if f.slug == "old-way"][0].reason)

    def test_deletion_is_off_unless_asked_for(self):
        quiet = AuditPolicy(gates=self.policy.gates)
        self.assertNotEqual(VERDICT_DELETE, verdicts(run_audit(self.store, quiet))["old-way"])

    def test_a_successor_that_was_marked_wrong_protects_the_original(self):
        # Otherwise the old answer is gone and the new one is known to be false.
        self.store.update_memory("new-way", {"status": "wrong"})
        self.assertNotEqual(VERDICT_DELETE, verdicts(run_audit(self.store, self.policy))["old-way"])

    def test_an_archived_successor_protects_the_original(self):
        self.store.archive_memory("new-way")
        self.assertNotEqual(VERDICT_DELETE, verdicts(run_audit(self.store, self.policy))["old-way"])

    def test_applying_deletion_keeps_the_body_in_revisions(self):
        run_audit(self.store, self.policy, apply=True)
        with self.assertRaises(Exception):
            self.store.get_memory("old-way")
        kept = self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM revisions WHERE project_id='demo'").fetchone()["n"]
        self.assertGreater(kept, 0)


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


class DuplicateTests(AuditCase):
    """Similarity nominates; only an agent decides."""

    def setUp(self):
        super().setUp()
        self.first = memory("cache-race-a", CACHE, ["area:x"])
        self.first["scope"]["files"] = ["Source/Cache.cpp"]
        self.second = memory("cache-race-b", CACHE + " Same thing, written twice.", ["area:x"])
        self.second["scope"]["files"] = ["Source/Cache.cpp"]

    def test_a_near_identical_pair_is_nominated_with_both_bodies(self):
        self.store.create_memory(self.first)
        self.store.create_memory(self.second)
        found = self.store.duplicate_candidates()
        self.assertEqual(1, found["count"])
        pair = found["candidates"][0]
        self.assertEqual({"cache-race-a", "cache-race-b"}, {m["id"] for m in pair["memories"]})
        self.assertGreater(pair["score"], 0.6)

    def test_memories_about_the_same_area_are_not_duplicates(self):
        self.store.create_memory(self.first)
        different = memory("shader-note", SHADER, ["area:x"])
        different["scope"]["files"] = ["Source/Cache.cpp"]
        self.store.create_memory(different)
        self.assertEqual(0, self.store.duplicate_candidates()["count"])

    def test_merging_keeps_everything_both_knew(self):
        self.first["pitfalls"] = ["Do not clear the cache mid-request."]
        self.second["triggers"] = ["auth refresh returns stale session"]
        self.store.create_memory(self.first)
        self.store.create_memory(self.second)

        self.store.merge_memories("cache-race-a", "cache-race-b", "Same lesson, worded differently.")

        kept = self.store.get_memory("cache-race-a")
        self.assertIn("Do not clear the cache mid-request.", kept["pitfalls"])
        self.assertIn("auth refresh returns stale session", kept["triggers"])

    def test_merging_adds_the_counters_rather_than_choosing(self):
        self.store.create_memory(self.first)
        self.store.create_memory(self.second)
        self.store.record_use(["cache-race-a"])
        self.store.record_use(["cache-race-b"])
        self.store.record_use(["cache-race-b"])

        self.store.merge_memories("cache-race-a", "cache-race-b", "One fact.")

        self.assertEqual(3, self.store.load_usage()["memories"]["cache-race-a"]["applied"])

    def test_the_merged_memory_is_archived_with_a_pointer_not_deleted(self):
        self.store.create_memory(self.first)
        self.store.create_memory(self.second)
        self.store.merge_memories("cache-race-a", "cache-race-b", "One fact.")

        self.assertEqual([], self.store.recall("cache invalidation", record=False)["memories"][1:])
        row = self.store.connection.execute(
            "SELECT archived_at, merged_into FROM memories WHERE slug='cache-race-b'").fetchone()
        self.assertTrue(row["archived_at"])
        self.assertEqual(self.store._uuid_for("cache-race-a"), row["merged_into"])
        self.assertEqual(CACHE + " Same thing, written twice.",
                         self.store.get_memory("cache-race-b")["description"])

    def test_a_merge_needs_a_stated_reason(self):
        self.store.create_memory(self.first)
        self.store.create_memory(self.second)
        with self.assertRaises(Exception):
            self.store.merge_memories("cache-race-a", "cache-race-b", "")
        with self.assertRaises(Exception):
            self.store.merge_memories("cache-race-a", "cache-race-a", "self")

    def test_archived_memories_are_not_nominated_again(self):
        self.store.create_memory(self.first)
        self.store.create_memory(self.second)
        self.store.merge_memories("cache-race-a", "cache-race-b", "One fact.")
        self.assertEqual(0, self.store.duplicate_candidates()["count"])
