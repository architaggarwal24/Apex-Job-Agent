# tests/test_automation_tab.py
"""
tests/test_automation_tab.py — the backend pieces behind Settings > Automation (the tab itself
was exercised in a real headless browser against the real API):

  * applier/autonomy.preview_rule and GET /api/automation/rules/{id}/preview: which queued
    jobs a rule would auto-submit and why not - the kill switch, assist mode, the rule's own
    conditions, its daily max counted across the preview, the daily apply limit (not in dry
    run), the "every answer known" condition deferred to review, and the rule's ledger stats;
    per user (another user's rule is 404, only your own queued jobs are previewed);
  * runner tokens report `online` (seen in the last minute) and never the token;
  * the Settings page has the Automation tab, and the settings moved there are not also
    edited on Agent Behavior.

Real SQLite in a temp directory, a fake clock for the runner tokens, no network, no LLM.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from applier import autonomy
from db.autonomy_db import AutonomyStore
from db.runner_db import SEEN_WINDOW_SECONDS, RunnerTokenStore

ROOT = Path(__file__).resolve().parent.parent

GH = "https://boards.greenhouse.io/acme/jobs/"


class FakeSettings:
    def __init__(self, **v):
        self.v = {"dry_run": "true", "quality_first": "false", "daily_apply_limit": "30",
                  "apply_mode": "auto", "apply_mode_overrides": "{}", **v}

    def read(self, key, default=""):
        return self.v.get(key, default)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AutonomyStore(1, Path(self.tmp.name) / "jobs.db")
        for i in range(4):                                       # a greenhouse record: 3 of 4 approved unchanged
            self.store.record_review_reached(f"l{i}", "greenhouse")
            self.store.close_attempt(f"l{i}", "approved_unchanged" if i < 3 else "approved_edited")
        self.store.set_enabled(True)
        self.rule = self.store.add_rule({"site_type": "greenhouse", "min_score": 80, "max_per_day": 2,
                                         "min_attempts": 3, "min_success_rate": 0.7, "require_all_answers_known": False})
        self.queued = [{"job_id": "a", "url": GH + "1", "ai_score": 95, "title": "A"},
                       {"job_id": "b", "url": GH + "2", "ai_score": 90, "title": "B"},
                       {"job_id": "c", "url": GH + "3", "ai_score": 85, "title": "C"},
                       {"job_id": "lo", "url": GH + "4", "ai_score": 50, "title": "Lo"},
                       {"job_id": "li", "url": "https://www.linkedin.com/jobs/view/9", "ai_score": 99, "title": "Li"}]

    def preview(self, rule=None, settings=None, applied_today=0):
        return autonomy.preview_rule(rule or self.rule, self.queued, store=self.store,
                                     settings=settings or FakeSettings(), applied_today=applied_today)

    def by_id(self, p):
        return {j["job_id"]: j for j in p["jobs"]}

    def test_which_jobs_would_submit_and_why_not(self):
        p = self.preview()
        j = self.by_id(p)
        self.assertEqual([x["job_id"] for x in p["jobs"] if x["would_submit"]], ["a", "b"])
        self.assertEqual(p["would_submit"], 2)
        self.assertIn("already auto-approved 2 today (max 2)", "; ".join(j["c"]["reasons"]))   # max_per_day counted across the preview
        self.assertTrue(any("below 80" in r for r in j["lo"]["reasons"]))
        self.assertTrue(any("rule is for greenhouse" in r for r in j["li"]["reasons"]))
        self.assertEqual(j["a"]["site_type"], "greenhouse")

    def test_kill_switch_off_blocks_everything(self):
        self.store.set_enabled(False)
        p = self.preview()
        self.assertEqual(p["would_submit"], 0)
        self.assertFalse(p["autonomy_enabled"])
        self.assertTrue(all("autonomy is switched off" in x["reasons"] for x in p["jobs"]))

    def test_assist_mode_is_never_auto_submitted(self):
        p = self.preview(settings=FakeSettings(apply_mode_overrides='{"greenhouse": "assist"}'))
        self.assertEqual(p["would_submit"], 0)
        self.assertIn("greenhouse is in assist mode: you apply by hand", self.by_id(p)["a"]["reasons"])

    def test_daily_apply_limit_applies_outside_dry_run(self):
        self.assertEqual(self.preview(settings=FakeSettings(dry_run="false"), applied_today=29)["would_submit"], 1)
        self.assertEqual(self.preview(settings=FakeSettings(dry_run="true"), applied_today=29)["would_submit"], 2)

    def test_disabled_rule_and_not_enough_evidence(self):
        self.assertEqual(self.preview(rule={**self.rule, "enabled": False})["would_submit"], 0)
        self.assertEqual(self.preview(rule={**self.rule, "min_success_rate": 0.8})["would_submit"], 0)
        self.assertEqual(self.preview(rule={**self.rule, "min_attempts": 5})["would_submit"], 0)

    def test_every_answer_known_is_judged_at_review(self):
        p = self.preview(rule={**self.rule, "require_all_answers_known": True})
        a = self.by_id(p)["a"]
        self.assertTrue(a["would_submit"])
        self.assertIn("checked at review", a["condition"])
        self.assertEqual(self.by_id(self.preview())["a"]["condition"], "")

    def test_stats_and_todays_count(self):
        p = self.preview()
        self.assertEqual((p["stats"]["reviewed"], p["stats"]["success_rate"]), (4, 0.75))
        self.assertEqual(p["auto_approved_today"], 0)
        self.store.record_review_reached("done-today", "greenhouse")
        self.store.close_attempt("done-today", "auto_approved", rule_id=self.rule["id"])
        p = self.preview()
        self.assertEqual(p["auto_approved_today"], 1)
        self.assertEqual(p["would_submit"], 1)                   # one of the day's 2 is already used
        star = self.store.add_rule({"site_type": "*", "min_score": 0, "max_per_day": 1})
        self.assertIsNone(self.preview(rule=star)["stats"])

    def test_preview_changes_nothing(self):
        before = (self.store.list_rules(), self.store.ledger())
        self.preview()
        self.assertEqual((self.store.list_rules(), self.store.ledger()), before)


class RunnerOnlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RunnerTokenStore(1, Path(self.tmp.name) / "jobs.db")

    def test_online_means_seen_in_the_last_minute(self):
        t = self.store.create("laptop", now=1000.0)
        self.assertNotIn("token", self.store.list(now=1000.0)[0])
        self.assertFalse(self.store.list(now=1000.0)[0]["online"])              # never seen
        self.store.touch(t["id"], now=2000.0)
        self.assertTrue(self.store.list(now=2000.0 + SEEN_WINDOW_SECONDS - 1)[0]["online"])
        self.assertFalse(self.store.list(now=2000.0 + SEEN_WINDOW_SECONDS + 1)[0]["online"])
        self.store.revoke(t["id"], now=2001.0)
        self.assertFalse(self.store.list(now=2001.0)[0]["online"])              # revoked is never online


try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class PreviewHttpTests(unittest.TestCase):
    def setUp(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self._saved = (cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE,
                       cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer, SettingsDB._instance)
        SettingsDB._instance = SettingsDB(path=tmp / "settings.db")
        cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR = tmp / "jobs.db", tmp / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP = "test-signing-secret-" + "x" * 40, False, False
        UsersDB._instance, auth._serializer = UsersDB(path=tmp / "users.db"), None
        from server.app import create_app
        self.app = create_app(ui_dir=tmp / "no-build")
        self.alice = TestClient(self.app, follow_redirects=False)
        self.alice.post("/api/auth/signup", json={"email": "alice@example.com", "password": "correct-horse-battery"})
        cfg.ALLOW_SIGNUP = True
        self.bob = TestClient(self.app, follow_redirects=False)
        self.bob.post("/api/auth/signup", json={"email": "bob@example.com", "password": "correct-horse-battery"})

    def tearDown(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB
        (cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP,
         UsersDB._instance, auth._serializer, SettingsDB._instance) = self._saved
        self.tmp.cleanup()

    def queue(self, uid, n, score):
        from db.jobs_db import JobsDB
        db = JobsDB(user_id=uid)
        j = db.add({"url": f"{GH}{uid}{n}", "title": f"Job {n}", "company": "Acme", "source": "linkedin"})
        db.update_score(j, score, "")
        db.transition(j, "queued", actor="user")
        return j

    def test_route_previews_only_your_own_queue_in_queue_order(self):
        low, high = self.queue(1, 1, 60), self.queue(1, 2, 90)
        self.queue(2, 1, 99)                                         # bob's queued job
        rid = self.alice.post("/api/automation/rules", json={"site_type": "greenhouse", "min_score": 80, "max_per_day": 5}).json()["id"]
        r = self.alice.get(f"/api/automation/rules/{rid}/preview")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual([j["job_id"] for j in body["jobs"]], [high, low])
        self.assertEqual(body["rule_id"], rid)
        self.assertFalse(body["autonomy_enabled"])
        self.assertEqual(self.bob.get(f"/api/automation/rules/{rid}/preview").status_code, 404)
        self.assertEqual(self.alice.get("/api/automation/rules/999/preview").status_code, 404)
        self.assertEqual(TestClient(self.app).get(f"/api/automation/rules/{rid}/preview").status_code, 401)

    def test_runner_token_list_carries_online_never_the_token(self):
        created = self.alice.post("/api/settings/runner-tokens", json={"label": "laptop"}).json()
        listed = self.alice.get("/api/settings/runner-tokens")
        self.assertNotIn(created["token"], listed.text)
        self.assertIs(listed.json()["tokens"][0]["online"], False)
        self.assertEqual(self.bob.get("/api/settings/runner-tokens").status_code, 403)


SRC = ROOT / "frontend" / "src"


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class SettingsNavTests(unittest.TestCase):
    """lib/settingsNav.js: five pages of folds, and every old one-section link still lands."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "settingsNav.mjs").write_text((SRC / "lib" / "settingsNav.js").read_text(encoding="utf-8"), encoding="utf-8")
            (tmp / "check.mjs").write_text((ROOT / "tests" / "fixtures" / "settings_nav_check.mjs").read_text(encoding="utf-8"), encoding="utf-8")
            r = subprocess.run(["node", str(tmp / "check.mjs")], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError(r.stderr[-2000:])
        cls.out = json.loads(r.stdout)

    def test_five_pages(self):
        self.assertEqual(self.out["pages"], ["start", "ai", "search", "applying", "you"])
        folds = [f for fs in self.out["folds"].values() for f in fs]
        self.assertEqual(len(folds), len(set(folds)), "a fold key belongs to one page")

    def test_old_links_still_land(self):
        want = {"appearance": ("you", "appearance"), "providers": ("ai", "providers"), "agent": ("applying", "pace"),
                "keys": ("search", "keys"), "apify": ("search", "keys"), "limits": ("applying", "limits"),
                "blacklist": ("applying", "skip"), "logins": ("applying", "logins"), "runners": ("applying", "runner"),
                "notifications": ("you", "notifications")}
        for k, (page, fold) in want.items():
            self.assertEqual(self.out["legacy"][k], {"page": page, "fold": fold}, k)
        for k, r in self.out["legacy"].items():
            self.assertIn(r["fold"], self.out["folds"][r["page"]], f"{k} lands on a fold that exists")

    def test_resolving(self):
        o = self.out
        self.assertEqual(o["page_and_fold"], {"page": "applying", "fold": "pace"})
        self.assertEqual(o["page_bad_fold"], {"page": "applying", "fold": ""})
        self.assertEqual((o["nothing"], o["junk"]), ({"page": "start", "fold": ""}, {"page": "start", "fold": ""}))
        self.assertEqual(o["href"], ["/settings?s=ai", "/settings?s=applying&open=modes"])
        self.assertEqual(o["places"], 5 + sum(len(f) for f in o["folds"].values()))
        self.assertEqual(o["place_fold"][1], "Settings › Applying › Where it runs")


class WiringTests(unittest.TestCase):
    def test_every_fold_is_rendered(self):
        page = (SRC / "app" / "settings" / "page.jsx").read_text(encoding="utf-8")
        nav = (SRC / "lib" / "settingsNav.js").read_text(encoding="utf-8")
        folds = re.findall(r"\['([a-z]+)', '[^']+'\]", nav[nav.index("SETTINGS_FOLDS"):nav.index("const LEGACY")])
        self.assertEqual(sorted(folds), sorted(re.findall(r"case '([a-z]+)':", page)))
        self.assertIn("resolveSettings(params.get('s'), params.get('open'))", page)   # page and fold are in the URL

    def test_every_settings_part_is_on_the_settings_page(self):
        page = (SRC / "app" / "settings" / "page.jsx").read_text(encoding="utf-8")
        for part in ("<QualityCaps", "<ApplyModes", "<Executor", "<PaceAndPractice", "<AiScoring", "<AutonomySection", "<Ledger",
                     "<AnswerBankSection", "<ResumesSection", "<RunnerTokensSection", "<NotificationsSection", "<Blacklist", "<Boards",
                     "<SourceList", "<Companies", "<Keys", "<Apify", "<SessionsTab", "<ProvidersTab", "<Appearance", "<Account", "<StartHere"):
            self.assertIn(part, page, part)

    def test_every_setting_is_edited_in_one_place(self):
        auto = (SRC / "components" / "settings" / "AutomationTab.jsx").read_text(encoding="utf-8")
        for key in ("daily_apply_limit", "auto_skip_score_threshold", "company_blacklist", "min_salary_lpa", "dry_run",
                    "enable_llm_scoring", "min_delay_between_applies", "max_delay_between_applies", "apply_mode", "executor"):
            self.assertEqual(len(re.findall(rf"useSettingsForm\(settings, \[[^\]]*'{key}'", auto)), 1, key)
        self.assertFalse((SRC / "components" / "settings" / "AgentTab.jsx").exists(), "the old Agent Behavior tab is gone")

    def test_owner_only_parts_are_read_only_for_others(self):
        parts = (ROOT / "frontend" / "src" / "components" / "settings" / "AutomationParts.jsx").read_text(encoding="utf-8")
        self.assertRegex(parts, r"<fieldset disabled=\{ownerOnly && !owner\}")


if __name__ == "__main__":
    unittest.main()
