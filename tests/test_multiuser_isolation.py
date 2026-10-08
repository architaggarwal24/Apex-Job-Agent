"""
tests/test_multiuser_isolation.py — proves one user can't see or touch
another user's jobs.

Run from the project root:   python3 -m unittest discover -s tests -v

Uses plain unittest (no pytest dependency) against a real throwaway
SQLite file, so these exercise the actual SQL — not mocks.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# SettingsDB imports API keys from the real environment and encrypts
# them, which would create the real data/.master.key. Use a throwaway key.
from cryptography.fernet import Fernet
os.environ["APP_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from db.jobs_db import JobsDB
from db.settings_db import SettingsDB


def job(url="https://www.linkedin.com/jobs/view/111", title="Backend Engineer",
        company="Acme", source="linkedin"):
    return {"url": url, "title": title, "company": company,
            "source": source, "location": "Bengaluru"}


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "jobs.db"
        # get_pending() consults the global SettingsDB singleton. Point it
        # at a throwaway file so these tests never create or read the
        # real data/settings.db.
        self._old_settings = SettingsDB._instance
        SettingsDB._instance = SettingsDB(path=Path(self.tmp.name) / "settings.db")
        self.alice = JobsDB(path=self.path, user_id=1)
        self.bob   = JobsDB(path=self.path, user_id=2)

    def tearDown(self):
        SettingsDB._instance = self._old_settings
        self.tmp.cleanup()

    # ── the multi-tenancy trap: same posting, two users ───────────

    def test_same_posting_is_independent_per_user(self):
        a = self.alice.add(job())
        b = self.bob.add(job())
        self.assertIsNotNone(a)
        self.assertIsNotNone(b, "Bob's copy was wrongly deduped against Alice's")
        self.assertNotEqual(a, b, "job_id must differ per user")

        self.alice.update_status(a, "applied")
        self.assertEqual(self.bob.get_by_id(b)["status"], "found",
                         "Alice applying must not change Bob's job")

    def test_dedup_still_works_within_one_user(self):
        self.assertIsNotNone(self.alice.add(job()))
        self.assertIsNone(self.alice.add(job()), "same URL twice should dedup")
        self.assertIsNone(
            self.alice.add(job(url="https://www.linkedin.com/jobs/view/222")),
            "same title+company+source should dedup")

    # ── reads ─────────────────────────────────────────────────────

    def test_reads_are_scoped(self):
        a = self.alice.add(job(url="https://x.com/a", title="Alice Job", company="A"))
        self.bob.add(job(url="https://x.com/b", title="Bob Job", company="B"))

        self.assertEqual([j["title"] for j in self.alice.get_all()], ["Alice Job"])
        self.assertEqual([j["title"] for j in self.bob.get_all()],   ["Bob Job"])
        self.assertEqual(len(self.alice.get_pending()), 1)
        self.assertEqual(self.alice.get_stats()["total"], 1)
        self.assertEqual(self.bob.get_stats()["total"], 1)

        self.assertEqual(self.alice.exists_bulk(["https://x.com/b"]), set(),
                         "Alice must not learn that Bob has scraped a URL")
        self.assertEqual(self.alice.exists_bulk(["https://x.com/a"]), {"https://x.com/a"})
        self.assertIsNone(self.bob.get_by_id(a), "Bob fetched Alice's job by ID")

    def test_unscoped_legacy_mode_sees_everything(self):
        self.alice.add(job(url="https://x.com/a", title="A", company="A"))
        self.bob.add(job(url="https://x.com/b", title="B", company="B"))
        self.assertEqual(len(JobsDB(path=self.path).get_all()), 2)

    # ── writes: the attack scenarios ──────────────────────────────

    def test_cannot_mutate_another_users_job(self):
        a = self.alice.add(job())

        self.bob.update_status(a, "skipped")
        self.bob.update_score(a, 99, "hacked")
        row = self.alice.get_by_id(a)
        self.assertEqual(row["status"], "found")
        self.assertIsNone(row["ai_score"])

    def test_cannot_write_into_another_users_job(self):
        a = self.alice.add(job())
        with self.assertRaises(LookupError):
            self.bob.add_note(a, "injected note")
        with self.assertRaises(LookupError):
            self.bob.add_stage(a, "offer")
        with self.assertRaises(LookupError):
            self.bob.save_brief(a, "injected brief")

        full = self.alice.get_by_id(a)
        self.assertEqual(full["notes"], [])
        self.assertEqual(full["stages"], [])
        self.assertEqual(full["brief"], "")

    # ── bulk destructive operations ───────────────────────────────

    def test_purge_duplicates_only_touches_own_rows(self):
        # Force a within-user duplicate for Alice by inserting the
        # legacy way (bypassing add()'s dedup), plus one Bob row.
        with self.alice._conn() as c:
            for i in range(2):
                c.execute("""INSERT INTO jobs (job_id,url_hash,title_co_key,title,
                    company,url,source,found_at,updated_at,user_id)
                    VALUES (?,?,?,?,?,?,?,?,?,1)""",
                    (f"dup{i}", f"h{i}", "dup|co", "Dup", "Co", f"u{i}",
                     "linkedin", "2026-01-01", "2026-01-01"))
        self.bob.add(job(url="https://x.com/b", title="Bob Job", company="B"))

        self.assertEqual(self.alice.purge_duplicates(), 1)
        self.assertEqual(len(self.alice.get_all()), 1)
        self.assertEqual(len(self.bob.get_all()), 1, "Bob's data was touched")

    def test_clear_all_only_touches_own_rows(self):
        self.alice.add(job(url="https://x.com/a", title="A", company="A"))
        self.bob.add(job(url="https://x.com/b", title="B", company="B"))
        self.assertEqual(self.alice.clear_all(), 1)
        self.assertEqual(len(self.alice.get_all()), 0)
        self.assertEqual(len(self.bob.get_all()), 1, "Alice's clear wiped Bob's jobs")

    def test_clear_all_keeps_applied_by_default(self):
        a = self.alice.add(job())
        self.alice.add_stage(a, "applied")
        self.assertEqual(self.alice.clear_all(), 0)
        self.assertEqual(len(self.alice.get_all()), 1)

    def test_full_wipe_removes_children_only_for_own_jobs(self):
        a = self.alice.add(job(url="https://x.com/a", title="A", company="A"))
        b = self.bob.add(job(url="https://x.com/b", title="B", company="B"))
        self.alice.add_stage(a, "applied"); self.alice.add_note(a, "n")
        self.bob.add_stage(b, "applied");   self.bob.add_note(b, "n")

        self.alice.clear_all(include_applied=True)

        self.assertEqual(self.alice.get_all(), [])
        self.assertEqual(self.alice.get_pipeline()["applied"], [])
        bob_full = self.bob.get_by_id(b)
        self.assertEqual(len(bob_full["stages"]), 1, "Bob's stage history was wiped")
        self.assertEqual(len(bob_full["notes"]), 1, "Bob's notes were wiped")

    # ── stage-derived views ───────────────────────────────────────

    def test_pipeline_stats_and_analytics_are_scoped(self):
        a = self.alice.add(job(url="https://x.com/a", title="A", company="A"))
        b = self.bob.add(job(url="https://x.com/b", title="B", company="B"))
        self.alice.add_stage(a, "applied")
        self.bob.add_stage(b, "applied"); self.bob.add_stage(b, "offer")

        self.assertEqual([r["title"] for r in self.alice.get_pipeline()["applied"]], ["A"])
        self.assertEqual(self.alice.get_pipeline()["offer"], [])
        self.assertEqual(len(self.bob.get_pipeline()["offer"]), 1)

        self.assertEqual(self.alice.get_stats()["pipeline"], {"applied": 1})
        self.assertEqual(self.bob.get_stats()["pipeline"],   {"offer": 1})

        funnel = {f["stage"]: f["count"] for f in self.alice.get_analytics()["funnel"]}
        self.assertEqual(funnel["Applied"], 1)
        self.assertEqual(funnel["Offer"], 0, "Alice's funnel counted Bob's offer")

    def test_daily_apply_limit_counters_are_per_user(self):
        a = self.alice.add(job())
        self.alice.add_stage(a, "applied")
        self.assertEqual(self.alice.count_applied_today(), 1)
        self.assertEqual(self.bob.count_applied_today(), 0,
                         "Bob would inherit Alice's daily apply budget")
        self.assertNotEqual(self.alice.get_last_applied_at(), "")
        self.assertEqual(self.bob.get_last_applied_at(), "")


class LegacyMigrationTests(unittest.TestCase):
    """A database created before multi-user support has no user_id
    column. It must upgrade cleanly, and user 1 (the person who was
    running this single-user) must inherit the existing jobs."""

    def test_pre_multiuser_database_upgrades_and_user1_adopts_rows(self):
        import importlib.util
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.db"

            # Build the DB with the ORIGINAL (pre-change) implementation.
            spec = importlib.util.spec_from_file_location(
                "jobs_db_legacy", Path(__file__).parent / "fixtures" / "jobs_db_legacy.py")
            legacy = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(legacy)
            old = legacy.JobsDB(path=path)
            old.add(job(url="https://x.com/old", title="Old Job", company="Old Co"))

            # A different user opening it first must NOT adopt the rows.
            bob = JobsDB(path=path, user_id=2)
            self.assertEqual(bob.get_all(), [], "user 2 grabbed pre-existing rows")

            # User 1 adopts them.
            alice = JobsDB(path=path, user_id=1)
            titles = [j["title"] for j in alice.get_all()]
            self.assertEqual(titles, ["Old Job"])

            # ...and they're now permanently hers, invisible to Bob.
            self.assertEqual(JobsDB(path=path, user_id=2).get_all(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
