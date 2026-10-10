# tests/test_review_http.py
"""
tests/test_review_http.py — structured review over HTTP: auth, the packet,
editing (and what it does to the answer bank), approving (dry run, daily
limit, owner-only) and rejecting, with per-user isolation throughout.

Run from the project root:   python3 -m unittest discover -s tests -v

Like tests/test_http_auth.py this skips itself when FastAPI/httpx aren't
installed. The behaviour behind the endpoints (packet building, flagging,
edit -> bank, submitting the reviewed values, reject) is tested without HTTP in
tests/test_review.py; approving never starts a browser here: the Orchestrator
is replaced by a mock.

NOTE: written in a sandbox without FastAPI, so NOT yet run. If one fails after
`pip install -r requirements.txt`, read it as a real finding first.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ["APP_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False

LONG = "I have built and run reliable systems for many years. " * 5


def snap(label, value="", *, type="text", key="", required=False, options=None):
    return {"label": label, "field_key": key, "type": type, "value": value, "options": options or [],
            "required": required, "page": 1, "hit": None}


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class ReviewHttpTests(unittest.TestCase):

    def setUp(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB

        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self._saved_settings = SettingsDB._instance
        SettingsDB._instance = SettingsDB(path=tmp / "settings.db")
        self._saved = (cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY,
                       cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer)
        cfg.JOBS_DB_PATH    = tmp / "jobs.db"
        cfg.PROFILE_DB_PATH = tmp / "profile.db"
        cfg.SESSIONS_DIR    = tmp / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        cfg.APP_SECRET_KEY  = "test-signing-secret-" + "x" * 40
        cfg.COOKIE_SECURE   = False
        cfg.ALLOW_SIGNUP    = False
        UsersDB._instance   = UsersDB(path=tmp / "users.db")
        auth._serializer    = None
        self._saved_dev_origin = os.environ.pop("APEX_DEV_UI_ORIGIN", None)

        from server.app import create_app
        self.app    = create_app(ui_dir=tmp / "no-build")
        self.client = TestClient(self.app, follow_redirects=False)      # alice = owner (user 1)
        self.client.post("/api/auth/signup",
                         json={"email": "alice@example.com", "password": "correct-horse-battery"})
        cfg.ALLOW_SIGNUP = True
        self.bob = TestClient(self.app, follow_redirects=False)         # bob (user 2)
        self.bob.post("/api/auth/signup",
                      json={"email": "bob@example.com", "password": "correct-horse-battery"})
        self.n = 0

    def tearDown(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB
        SettingsDB._instance = self._saved_settings
        (cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE,
         cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer) = self._saved
        os.environ.pop("APEX_DEV_UI_ORIGIN", None)
        if self._saved_dev_origin is not None:
            os.environ["APEX_DEV_UI_ORIGIN"] = self._saved_dev_origin
        self.tmp.cleanup()

    # ── helpers ───────────────────────────────────────────────────

    def in_review(self, uid=1, snaps=None, stage="ready_for_review", company="Acme Corp"):
        """A job at `stage` with a review packet (and an apply session)."""
        from applier import review
        from applier.session import ApplicationSession
        from db.jobs_db import JobsDB
        from db.review_db import ReviewStore
        self.n += 1
        db = JobsDB(user_id=uid)
        jid = db.add({"url": f"https://example.com/{uid}/{self.n}", "title": f"Role {self.n}",
                      "company": company, "source": "linkedin"})
        db.transition(jid, "preparing")
        if stage == "ready_for_review":
            db.transition(jid, "ready_for_review")
        elif stage == "needs_you":
            db.transition(jid, "needs_you", reason="question")
        fields = review.build_fields(snaps or [
            snap("Expected CTC", "", required=True), snap("Why do you want to join Acme?", LONG, type="textarea"),
            snap("Willing to relocate", "Maybe", type="select", options=["Yes", "No"]),
            snap("Notice period", "30 days", key="np")], {"np": "30 days"})
        ReviewStore(uid).save(jid, review.build_packet(jid, "https://example.com/apply", "resume.pdf",
                                                       ["review_1.png"], fields))
        ApplicationSession(job_id=jid, job_url="u", job_title="t", company=company, source="linkedin",
                           state="screenshot_review").save()
        return jid

    def get(self, jid, client=None):
        return (client or self.client).get(f"/api/applications/{jid}/review")

    def edit(self, jid, field_id="expected-ctc", value="24 LPA", client=None, **kw):
        return (client or self.client).post(f"/api/applications/{jid}/review/edit",
                                            json={"field_id": field_id, "value": value, **kw})

    def approve(self, jid, client=None):
        return (client or self.client).post(f"/api/applications/{jid}/review/approve")

    def reject(self, jid, client=None, **body):
        return (client or self.client).post(f"/api/applications/{jid}/review/reject", json=body)

    def settings(self):
        from db.settings_db import SettingsDB
        return SettingsDB.get()

    def stage(self, jid, uid=1):
        from db.jobs_db import JobsDB
        return JobsDB(user_id=uid).get_stage(jid)

    # ── auth ──────────────────────────────────────────────────────

    def test_every_endpoint_requires_login(self):
        jid = self.in_review()
        anon = TestClient(self.app, follow_redirects=False)
        self.assertEqual(anon.get(f"/api/applications/{jid}/review").status_code, 401)
        for tail, body in (("edit", {"field_id": "x", "value": "y"}), ("approve", {}), ("reject", {})):
            self.assertEqual(anon.post(f"/api/applications/{jid}/review/{tail}", json=body).status_code, 401, tail)

    # ── the packet ────────────────────────────────────────────────

    def test_get_returns_the_packet_with_the_documented_fields(self):
        jid = self.in_review()
        r = self.get(jid)
        self.assertEqual(r.status_code, 200)
        p = r.json()
        self.assertEqual((p["job_id"], p["url"], p["resume"], p["screenshots"], p["stage"]),
                         (jid, "https://example.com/apply", "resume.pdf", ["review_1.png"], "ready_for_review"))
        self.assertEqual(p["screenshot_urls"], ["/screenshots/review_1.png"])
        self.assertEqual([f["id"] for f in p["fields"]],
                         ["expected-ctc", "why-want-join-acme", "willing-relocate", "notice-period"])
        self.assertEqual(set(p["fields"][0]), {"id", "label", "type", "value", "source", "confidence", "flagged",
                                               "reason", "required", "options"})
        self.assertEqual(p["flagged"], 3)
        by_id = {f["id"]: f for f in p["fields"]}
        self.assertEqual((by_id["expected-ctc"]["reason"], by_id["notice-period"]["source"]),
                         ("required but empty", "profile"))
        self.assertEqual((p["title"], p["company"]), ("Role 1", "Acme Corp"))

    def test_unknown_application_or_no_packet_is_404(self):
        self.assertEqual(self.get("nope").status_code, 404)
        from db.jobs_db import JobsDB
        j = JobsDB(user_id=1).add({"url": "https://example.com/x", "title": "T", "company": "C", "source": "l"})
        self.assertEqual(self.get(j).status_code, 404)

    def test_you_cannot_see_edit_approve_or_reject_someone_elses_review(self):
        jid = self.in_review(uid=1)
        self.assertEqual(self.get(jid, self.bob).status_code, 404)
        self.assertEqual(self.edit(jid, client=self.bob).status_code, 404)
        self.assertEqual(self.reject(jid, client=self.bob).status_code, 404)
        self.assertEqual(self.approve(jid, client=self.bob).status_code, 403)       # owner-only comes first
        self.assertEqual(self.stage(jid), "ready_for_review")
        self.assertEqual(self.get(jid).json()["fields"][0]["value"], "")

    def test_a_packet_under_someone_elses_job_id_gives_nothing_away(self):
        """Even if Bob somehow holds a packet row for Alice's job id, the job is
        not his: no title, company or stage is revealed."""
        from db.review_db import ReviewStore
        jid = self.in_review(uid=1)
        ReviewStore(2).save(jid, {"fields": [], "screenshots": [], "job_id": jid})
        self.assertEqual(self.get(jid, self.bob).status_code, 404)
        self.assertEqual(self.reject(jid, client=self.bob).status_code, 404)
        self.assertEqual(self.edit(jid, client=self.bob).status_code, 404)

    def test_each_user_reads_only_their_own_packet(self):
        mine = self.in_review(uid=1, snaps=[snap("Expected CTC", "20 LPA")])
        theirs = self.in_review(uid=2, snaps=[snap("Expected CTC", "35 LPA")])
        self.assertEqual(self.get(mine).json()["fields"][0]["value"], "20 LPA")
        self.assertEqual(self.get(theirs, self.bob).json()["fields"][0]["value"], "35 LPA")
        self.assertEqual(self.get(theirs).status_code, 404)
        self.assertEqual(self.get(mine, self.bob).status_code, 404)
        self.assertEqual(self.edit(theirs, "expected-ctc", "36 LPA", client=self.bob).status_code, 200)
        self.assertEqual(self.get(mine).json()["fields"][0]["value"], "20 LPA", "Alice's packet is untouched")

    # ── edit ──────────────────────────────────────────────────────

    def test_an_edit_updates_the_packet_the_answer_bank_and_the_timeline(self):
        jid = self.in_review()
        r = self.edit(jid, value="24 LPA")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        f = body["field"]
        self.assertEqual((f["value"], f["source"], f["confidence"], f["flagged"]), ("24 LPA", "user", 1.0, False))
        self.assertEqual((body["remembered"]["scope"], body["remembered"]["company"]), ("global", ""))
        self.assertEqual(body["flagged"], 2)
        again = self.get(jid).json()
        self.assertEqual(again["fields"][0]["value"], "24 LPA")
        self.assertEqual(again["flagged"], 2)
        bank = self.client.get("/api/answer-bank").json()
        self.assertEqual([(e["question_raw"], e["answer"], e["source"]) for e in bank], [("Expected CTC", "24 LPA", "user")])
        steps = [e for e in self.client.get(f"/api/applications/{jid}").json()["events"] if e["kind"] == "step"]
        self.assertEqual(steps[-1]["detail"]["step"], "review_edit")
        self.assertNotIn("24 LPA", str(steps[-1]), "the value itself is not written to the timeline")

    def test_the_remember_flag_decides_what_the_bank_gets(self):
        jid = self.in_review()
        names_company = self.edit(jid, "why-want-join-acme", "Great product").json()["remembered"]
        self.assertEqual((names_company["scope"], names_company["company"]), ("company", "Acme Corp"))
        forced_global = self.edit(jid, "why-want-join-acme", "Great product", remember="global").json()["remembered"]
        self.assertEqual(forced_global["scope"], "global")
        forced_company = self.edit(jid, "notice-period", "15 days", remember="company").json()["remembered"]
        self.assertEqual(forced_company["scope"], "company")
        before = len(self.client.get("/api/answer-bank").json())
        once = self.edit(jid, "expected-ctc", "24 LPA", remember="once").json()
        self.assertIsNone(once["remembered"])
        self.assertEqual(len(self.client.get("/api/answer-bank").json()), before)
        self.assertEqual(self.get(jid).json()["fields"][0]["value"], "24 LPA", "still edited in the packet")
        self.assertIsNone(self.edit(jid, "expected-ctc", "", remember="auto").json()["remembered"], "a cleared value is not remembered")

    def test_a_correction_to_a_saved_answer_updates_it_in_place_and_keeps_its_policy(self):
        self.client.post("/api/answer-bank", json={"question": "Expected CTC", "answer": "20 LPA", "policy": "ask"})
        jid = self.in_review()
        self.edit(jid, value="24 LPA")
        (e,) = self.client.get("/api/answer-bank").json()
        self.assertEqual((e["answer"], e["policy"]), ("24 LPA", "ask"))

    def test_bad_edits_are_refused_and_change_nothing(self):
        jid = self.in_review()
        bad = [({"value": "x"}, 400), ({"field_id": "", "value": "x"}, 400), ({"field_id": 5, "value": "x"}, 400),
               ({"field_id": "expected-ctc"}, 400), ({"field_id": "expected-ctc", "value": None}, 400),
               ({"field_id": "expected-ctc", "value": ["a"]}, 400), ({"field_id": "expected-ctc", "value": True}, 400),
               ({"field_id": "expected-ctc", "value": "x", "remember": "sometimes"}, 400),
               ({"field_id": "expected-ctc", "value": "x" * 5001}, 400),
               ({"field_id": "willing-relocate", "value": "Perhaps"}, 400),
               ({"field_id": "no-such-field", "value": "x"}, 404)]
        for body, code in bad:
            r = self.client.post(f"/api/applications/{jid}/review/edit", json=body)
            self.assertEqual(r.status_code, code, str(body))
        self.assertEqual(self.client.get("/api/answer-bank").json(), [])
        self.assertEqual(self.get(jid).json()["fields"][0]["value"], "")
        self.assertEqual(self.edit(jid, "willing-relocate", "yes").json()["field"]["value"], "Yes")

    def test_a_number_is_a_fine_value(self):
        jid = self.in_review()
        self.assertEqual(self.edit(jid, value=24).json()["field"]["value"], "24")

    def test_you_can_only_edit_while_the_application_is_in_review(self):
        waiting = self.in_review(stage="needs_you")
        self.assertEqual(self.edit(waiting).status_code, 409)
        done = self.in_review()
        self.reject(done)
        self.assertEqual(self.edit(done).status_code, 409)

    def test_an_edit_is_refused_while_something_is_running_on_the_application(self):
        from server.routes import apply as apply_routes
        jid = self.in_review()
        self.assertTrue(apply_routes._acquire(jid))
        try:
            self.assertEqual(self.edit(jid).status_code, 409)
        finally:
            apply_routes._release(jid)
        self.assertEqual(self.edit(jid).status_code, 200, "and the lock is not left held")

    # ── approve ───────────────────────────────────────────────────

    def test_approving_is_owner_only_and_needs_a_packet_and_the_review_stage(self):
        jid = self.in_review()
        self.assertEqual(self.approve(jid, self.bob).status_code, 403)
        waiting = self.in_review(stage="needs_you")
        self.assertEqual(self.approve(waiting).status_code, 409)
        self.assertEqual(self.approve("nope").status_code, 404)
        from db.jobs_db import JobsDB
        bare = JobsDB(user_id=1).add({"url": "https://example.com/bare", "title": "T", "company": "C", "source": "l"})
        self.assertEqual(self.approve(bare).status_code, 404)

    def test_approving_a_dry_run_submits_nothing_for_real_and_skips_the_daily_limit(self):
        self.settings().write("dry_run", "true")
        for i in range(10):                                         # the quality-first cap is already used up
            self.applied_job(i)
        jid = self.in_review()
        with mock.patch("applier.session.Orchestrator") as orchestrator:
            r = self.approve(jid)
        self.assertEqual((r.status_code, r.json()), (200, {"status": "submitting", "dry_run": True}))
        orchestrator.return_value.submit.assert_called_once()
        steps = [e["detail"] for e in self.client.get(f"/api/applications/{jid}").json()["events"] if e["kind"] == "step"]
        self.assertEqual(steps[-1], {"dry_run": True, "step": "review_approved", "message": "Approved in review"})

    def applied_job(self, i):
        from db.jobs_db import JobsDB
        db = JobsDB(user_id=1)
        j = db.add({"url": f"https://example.com/done/{i}", "title": f"Done {i}", "company": f"Co {i}", "source": "l"})
        db.transition(j, "submitted", actor="user")

    def test_a_real_approval_past_the_daily_limit_is_a_429_and_starts_nothing(self):
        self.settings().write("dry_run", "false")
        for i in range(10):
            self.applied_job(i)
        jid = self.in_review()
        with mock.patch("applier.session.Orchestrator") as orchestrator:
            r = self.approve(jid)
        self.assertEqual(r.status_code, 429)
        orchestrator.return_value.submit.assert_not_called()
        self.assertEqual(self.stage(jid), "ready_for_review")
        # the job lock was released: it can be approved once the limit is not in the way
        self.settings().write("dry_run", "true")
        with mock.patch("applier.session.Orchestrator") as orchestrator:
            self.assertEqual(self.approve(jid).status_code, 200)

    def test_a_real_approval_under_the_limit_goes_ahead(self):
        self.settings().write("dry_run", "false")
        jid = self.in_review()
        with mock.patch("applier.session.Orchestrator") as orchestrator:
            r = self.approve(jid)
        self.assertEqual((r.status_code, r.json()), (200, {"status": "submitting", "dry_run": False}))
        orchestrator.return_value.submit.assert_called_once()

    def test_approving_twice_at_once_is_a_409(self):
        from server.routes import apply as apply_routes
        self.settings().write("dry_run", "true")
        jid = self.in_review()
        self.assertTrue(apply_routes._acquire(jid))
        try:
            with mock.patch("applier.session.Orchestrator"):
                self.assertEqual(self.approve(jid).status_code, 409)
        finally:
            apply_routes._release(jid)

    # ── reject ────────────────────────────────────────────────────

    def test_rejecting_skips_the_application_with_the_reason(self):
        jid = self.in_review()
        r = self.reject(jid, reason="Salary is too low")
        self.assertEqual((r.status_code, r.json()), (200, {"status": "skipped", "reason": "Salary is too low"}))
        self.assertEqual(self.stage(jid), "skipped")
        events = self.client.get(f"/api/applications/{jid}").json()["events"]
        last = [e for e in events if e["kind"] == "transition"][-1]
        self.assertEqual((last["from_stage"], last["to_stage"], last["actor"], last["detail"]["note"]),
                         ("ready_for_review", "skipped", "user", "Salary is too low"))
        self.assertEqual(self.get(jid).status_code, 200, "the packet stays, as a record of what was filled")
        from applier.session import ApplicationSession
        self.assertEqual(ApplicationSession.load(jid).state, "skipped")
        from server.routes import apply as apply_routes
        self.assertTrue(apply_routes._acquire(jid), "the lock must not be left held after a reject")
        apply_routes._release(jid)

    def test_the_reason_is_optional_trimmed_and_capped(self):
        a = self.in_review()
        self.assertEqual(self.reject(a).json()["reason"], "Rejected in review")
        b = self.in_review()
        self.assertEqual(self.reject(b, reason="   ").json()["reason"], "Rejected in review")
        c = self.in_review()
        self.assertEqual(len(self.reject(c, reason="x" * 900).json()["reason"]), 500)
        d = self.in_review()
        self.assertEqual(self.reject(d, reason=["no"]).status_code, 400)
        self.assertEqual(self.stage(d), "ready_for_review")

    def test_only_an_application_in_review_can_be_rejected(self):
        waiting = self.in_review(stage="needs_you")
        self.assertEqual(self.reject(waiting).status_code, 409)
        jid = self.in_review()
        self.assertEqual(self.reject(jid).status_code, 200)
        self.assertEqual(self.reject(jid).status_code, 409)
        self.assertEqual(self.approve(jid).status_code, 409)

    def test_a_reject_is_refused_while_something_is_running(self):
        from server.routes import apply as apply_routes
        jid = self.in_review()
        self.assertTrue(apply_routes._acquire(jid))
        try:
            self.assertEqual(self.reject(jid).status_code, 409)
        finally:
            apply_routes._release(jid)
        self.assertEqual(self.stage(jid), "ready_for_review")


if __name__ == "__main__":
    unittest.main(verbosity=2)
