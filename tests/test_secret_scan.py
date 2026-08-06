"""The credential scan, and the promotion gate built on it."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from project_memory_mcp import federation, secret_scan
from project_memory_mcp.sqlite_store import SqliteMemoryStore, StoreError


#: Assembled at import rather than written as a literal. Stripe's own published
#: sample value is still Stripe-key-shaped, so GitHub's push protection blocks a
#: commit containing it - the same class of check this module implements,
#: reaching the same verdict on the same string. Splitting it keeps the fixture
#: exercising the rule without making the repository unpushable.
STRIPE = "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc"


def memory(memory_id, description, facts=None, pitfalls=None):
    return {
        "schema_version": 1, "id": memory_id, "status": "active", "description": description,
        "tags": [], "labels": ["area:x"],
        "scope": {"project": "p", "area": "a", "files": [], "applies_to": []},
        "triggers": ["trigger for " + memory_id],
        "remembered_facts": facts or [description],
        "solution_pattern": [], "pitfalls": pitfalls or [],
        "evidence": {"created_from_task": "t", "last_validated": "2026-01-01"},
        "relationships": {"related": [], "supersedes": [], "superseded_by": []},
    }


class ScanTests(unittest.TestCase):
    def rules(self, text):
        return {f.rule for f in secret_scan.scan_text(text, "field")}

    # --------------------------------------------------------------- it catches

    def test_vendor_prefixes_are_caught(self):
        for text, rule in [
            (f"STRIPE_KEY={STRIPE}", "stripe key"),
            ("token ghp_16C7e42F292c6912E7710c838347Ae178B4a", "github token"),
            ("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", "aws access key id"),
            ("use AIzaSyD-1234567890abcdefghijklmnopqrstuv as the key", "google api key"),
            ("slack bot token xoxb-123456789012-abcdefghijkl", "slack token"),
        ]:
            with self.subTest(rule=rule):
                self.assertIn(rule, self.rules(text))

    def test_structural_shapes_are_caught_without_any_name_nearby(self):
        # No "key" or "token" in these strings: structural rules must stand
        # alone, because that is what makes them the reliable tier.
        pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1r\n"
        self.assertIn("private key", self.rules(pem))
        jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
               "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
        self.assertIn("json web token", self.rules(jwt))
        self.assertIn("connection string",
                      self.rules("connect to postgres://app:hunter2@db.internal:5432/main"))

    def test_high_entropy_needs_a_credential_name_beside_it(self):
        blob = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MHF3ZXJ0eXVpb3A"
        self.assertEqual(set(), self.rules(f"the build emits {blob} into the log"))
        self.assertIn("high-entropy string", self.rules(f"SESSION_SECRET={blob}"))

    # ------------------------------------------------------------- it does not

    def test_a_memory_about_where_a_secret_lives_is_clean(self):
        # The whole point of the rewrite: this memory is useful and must survive.
        self.assertEqual(set(), self.rules(
            "The staging API key lives in .env.staging as STRIPE_KEY, rotated quarterly by "
            "the platform team; CI reads it from the vault path stripe-staging."))

    def test_prose_alone_is_never_a_finding(self):
        # English prose measures ~4.3 bits/char, higher than a real GitHub
        # token. If entropy ran unconditioned, this sentence would be a hit.
        self.assertEqual(set(), self.rules(
            "Do not remember secrets, credentials, tokens, or personal data."))

    def test_commit_hashes_survive_even_beside_the_word_token(self):
        # A 40-char SHA scores ~3.95 against a 3.0 hex threshold, and this
        # store's memories are largely about commits.
        self.assertEqual(set(), self.rules(
            "Fixed in 6038ba6f4c1d9e2a7b3c8d5e0f1a2b3c4d5e6f70 on main"))

    def test_our_own_fingerprints_are_allowlisted(self):
        # These travel in author_key on every promoted memory, and a public key
        # is high-entropy precisely so it can be published.
        text = ("Authored by laptop-a, key "
                "SHA256:47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU")
        self.assertEqual(set(), self.rules(text))

    def test_key_material_fields_are_skipped_outright(self):
        # Named for SKIP_FIELDS but it has to actually depend on it. With a
        # SHA256: fingerprint this passed either way, because ALLOWED strips
        # those before any rule sees them - so the test proved the allowlist
        # worked and said nothing about the field skip. A vendor-prefixed value
        # trips a rule that needs no name nearby, so only the skip can save it.
        # Field names spelled out rather than read from SKIP_FIELDS. Iterating
        # the constant under test meant emptying it produced an empty loop and a
        # passing test - the same mistake one layer down.
        skipped = ("author_key", "fingerprint", "public_key", "uuid")
        self.assertTrue(set(skipped) <= set(secret_scan.SKIP_FIELDS))
        for field in skipped:
            with self.subTest(field=field):
                body = memory("m", "A normal memory.")
                body[field] = "ghp_" + "16C7e42F292c6912E7710c838347Ae178B4a"
                self.assertEqual([], secret_scan.scan(body))

    def test_the_same_value_in_an_ordinary_field_is_still_caught(self):
        # The other half: skipping is scoped to those fields and nothing else.
        body = memory("m", "A normal memory.")
        body["remembered_facts"] = ["ghp_" + "16C7e42F292c6912E7710c838347Ae178B4a"]
        self.assertEqual(["github token"], [f.rule for f in secret_scan.scan(body)])

    # -------------------------------------------------------------- reporting

    def test_the_finding_never_repeats_the_secret(self):
        secret = STRIPE
        findings = secret_scan.scan_text(f"STRIPE_KEY={secret}", "facts[0]")
        self.assertTrue(findings)
        for finding in findings:
            self.assertNotIn(secret, finding.describe())
            self.assertNotIn(secret[8:], finding.excerpt)
        message = secret_scan.explain("m", findings)
        self.assertNotIn(secret, message)
        self.assertIn("allow_secrets=true", message)

    def test_scan_names_the_field_it_found(self):
        body = memory("m", "Fine.", pitfalls=[f"never paste {STRIPE}"])
        findings = secret_scan.scan(body)
        self.assertEqual(["pitfalls[0]"], [f.field for f in findings])

    def test_findings_are_capped(self):
        leaks = [f"KEY=sk_live_{'ab12cd34ef56gh78'}{n:04d}" for n in range(60)]
        body = memory("m", "Fine.", facts=leaks)
        self.assertLessEqual(len(secret_scan.scan(body)), secret_scan.MAX_FINDINGS)


class PromotionGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SqliteMemoryStore(Path(self.tmp.name) / "m.db", "demo")
        self.addCleanup(self.store.close)
        self.store.add_label("area:x", "x")
        federation.add_remote(self.store.connection, "team", "http://127.0.0.1:9/",
                              "the team server")

    def _public(self, memory_id, body):
        self.store.create_memory(body)
        self.store.set_visibility(memory_id, "public")

    def test_promotion_is_refused_when_the_body_carries_a_credential(self):
        self._public("leaky", memory(
            "leaky", "How to reach the billing sandbox.",
            facts=[f"Set STRIPE_KEY={STRIPE} before running it."]))
        with self.assertRaises(StoreError) as caught:
            federation.promote(self.store, "leaky", "team", force=True)
        self.assertIn("stripe key", str(caught.exception))
        # And the refusal does not leak what it refused.
        self.assertNotIn(STRIPE, str(caught.exception))

    def test_allow_secrets_is_the_only_way_past_and_force_is_not(self):
        # force overrides the tier gate; it must not also wave through a
        # credential, or the two judgments collapse into one.
        self._public("leaky", memory(
            "leaky", "Billing sandbox access.",
            facts=[f"STRIPE_KEY={STRIPE}"]))
        with self.assertRaises(StoreError):
            federation.promote(self.store, "leaky", "team", force=True)
        # With allow_secrets it passes every gate and reaches the outbox.
        result = federation.promote(self.store, "leaky", "team", force=True, allow_secrets=True)
        self.assertEqual("leaky", result["queued"])

    def test_a_clean_memory_promotes(self):
        self._public("clean", memory(
            "clean", "Where the staging key lives.",
            facts=["It is STRIPE_KEY in .env.staging, rotated quarterly by the platform team."]))
        result = federation.promote(self.store, "clean", "team", force=True)
        self.assertEqual("clean", result["queued"])

    def test_promotion_targets_warns_before_the_attempt(self):
        self._public("leaky", memory(
            "leaky", "Billing sandbox access.",
            facts=[f"STRIPE_KEY={STRIPE}"]))
        targets = federation.promotion_targets(self.store, "leaky")
        self.assertIn("secret", targets["blocked"])
        self.assertTrue(any("stripe key" in f for f in targets["secret_findings"]))

    def test_the_outbox_holds_a_memory_that_gained_a_secret_after_queueing(self):
        # The body is re-read at send time, so the scan has to run there too.
        self._public("clean", memory(
            "clean", "Where the staging key lives.",
            facts=["It is STRIPE_KEY in .env.staging."]))
        federation.promote(self.store, "clean", "team", force=True)
        self.store.update_memory("clean", {
            "remembered_facts": [f"STRIPE_KEY={STRIPE}"]})
        result = federation.deliver_outbox(self.store)
        self.assertEqual(["clean"], result["held_for_secrets"])
        self.assertEqual([], result["sent"])


if __name__ == "__main__":
    unittest.main()
