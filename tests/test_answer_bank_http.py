# tests/test_answer_bank_http.py
"""
tests/test_answer_bank_http.py — the answer bank over HTTP: auth, CRUD and its
status codes, resolve, per-user isolation, the read-only profile entries, and
the remember flag on the answer endpoint.

Run from the project root:   python3 -m unittest discover -s tests -v

Like tests/test_http_auth.py this skips itself when FastAPI/httpx aren't
installed. The matching rules themselves are tested without HTTP in
tests/test_answer_bank.py and tests/test_answer_fill.py.

NOTE: written in a sandbox without FastAPI, so NOT yet run. If one fails after
`pip install -r requirements.txt`, read it as a real finding first.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ["APP_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class AnswerBankHttpTests(unittest.TestCase):

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

    def post(self, body, client=None):
        return (client or self.client).post("/api/answer-bank", json=body)

    def resolve(self, question, company="", client=None):
        return (client or self.client).post("/api/answer-bank/resolve",
                                            json={"question": question, "company": company})

    def profile(self, **values):
        from db.profile_db import ProfileDB
        db = ProfileDB()
        for k, v in values.items():
            db.set(k, v)

    # ── auth ──────────────────────────────────────────────────────

    def test_every_endpoint_requires_login(self):
        anon = TestClient(self.app, follow_redirects=False)
        calls = [("get", "/api/answer-bank"), ("post", "/api/answer-bank"),
                 ("post", "/api/answer-bank/resolve"), ("get", "/api/answer-bank/1"),
                 ("put", "/api/answer-bank/1"), ("delete", "/api/answer-bank/1")]
        for method, path in calls:
            r = getattr(anon, method)(path) if method in ("get", "delete") else getattr(anon, method)(path, json={})
            self.assertEqual(r.status_code, 401, (method, path))

    def test_the_bank_is_your_own_data_so_any_signed_in_user_may_manage_theirs(self):
        self.assertEqual(self.post({"question": "Expected CTC", "answer": "35 LPA"}, self.bob).status_code, 200)

    # ── CRUD ──────────────────────────────────────────────────────

    def test_create_read_update_delete(self):
        r = self.post({"question": "What is your notice period?", "answer": "30 days"})
        self.assertEqual(r.status_code, 200)
        e = r.json()
        self.assertEqual((e["scope"], e["company"], e["policy"], e["source"], e["uses"], e["question_norm"]),
                         ("global", "", "always", "user", 0, "notice period"))
        self.assertNotIn("user_id", e)
        self.assertEqual(self.client.get(f"/api/answer-bank/{e['id']}").json(), e)
        self.assertEqual([x["id"] for x in self.client.get("/api/answer-bank").json()], [e["id"]])

        r = self.client.put(f"/api/answer-bank/{e['id']}", json={"answer": "45 days", "policy": "ask"})
        self.assertEqual((r.status_code, r.json()["answer"], r.json()["policy"], r.json()["source"]),
                         (200, "45 days", "ask", "user"))
        r = self.client.put(f"/api/answer-bank/{e['id']}",
                            json={"scope": "company", "company": "Acme Corp"})
        self.assertEqual((r.json()["scope"], r.json()["company"]), ("company", "Acme Corp"))

        self.assertEqual(self.client.delete(f"/api/answer-bank/{e['id']}").json(), {"status": "deleted"})
        self.assertEqual(self.client.get(f"/api/answer-bank/{e['id']}").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/answer-bank/{e['id']}").status_code, 404)
        self.assertEqual(self.client.put(f"/api/answer-bank/{e['id']}", json={"answer": "x"}).status_code, 404)

    def test_saving_the_same_question_again_updates_it_and_keeps_its_policy(self):
        a = self.post({"question": "Night shifts?", "answer": "Yes", "policy": "ask"}).json()
        b = self.post({"question": "night shifts", "answer": "Weekdays only"}).json()
        self.assertEqual((b["id"], b["answer"], b["policy"]), (a["id"], "Weekdays only", "ask"))
        self.assertEqual(len(self.client.get("/api/answer-bank").json()), 1)

    def test_bad_input_is_a_400_and_stores_nothing(self):
        bad = [{}, {"question": "Q?", "answer": ""}, {"question": "", "answer": "x"}, {"answer": "x"},
               {"question": "Expected CTC"}, {"question": "?!", "answer": "x"},
               {"question": "Expected CTC", "answer": "x", "scope": "team"},
               {"question": "Expected CTC", "answer": "x", "scope": "company"},
               {"question": "Expected CTC", "answer": "x", "policy": "sometimes"},
               {"question": "Expected CTC", "answer": "x", "source": "profile"},
               {"question": "Expected CTC", "answer": "x", "source": "robot"},
               {"question": ["a"], "answer": "x"}, {"question": "Expected CTC", "answer": True},
               {"question": "q" * 1001, "answer": "x"}, {"question": "Expected CTC", "answer": "a" * 5001}]
        for body in bad:
            self.assertEqual(self.post(body).status_code, 400, str(body)[:80])
        self.assertEqual(self.client.get("/api/answer-bank").json(), [])

    def test_numbers_are_accepted_as_answers(self):
        e = self.post({"question": "Expected CTC", "answer": 20}).json()
        self.assertEqual(e["answer"], "20")

    def test_update_errors(self):
        a = self.post({"question": "Expected CTC", "answer": "20"}).json()
        b = self.post({"question": "Notice period", "answer": "30"}).json()
        self.assertEqual(self.client.put(f"/api/answer-bank/{b['id']}", json={"question": "Expected CTC"}).status_code, 409)
        for body in ({"answer": ""}, {"scope": "company"}, {"policy": "x"}, {"scope": "nope"}, {"answer": ["x"]}):
            self.assertEqual(self.client.put(f"/api/answer-bank/{a['id']}", json=body).status_code, 400, body)
        self.assertEqual(self.client.get(f"/api/answer-bank/{a['id']}").json()["answer"], "20")
        for path in ("abc", "1.5", "profile"):
            self.assertEqual(self.client.get(f"/api/answer-bank/{path}").status_code, 404, path)

    def test_listing_filters(self):
        self.post({"question": "Notice period", "answer": "30 days"})
        self.post({"question": "Why Acme?", "answer": "Because", "scope": "company", "company": "Acme Corp"})
        self.post({"question": "Expected CTC", "answer": "20 LPA", "source": "ai"})
        qs = lambda query: sorted(e["question_raw"] for e in self.client.get("/api/answer-bank" + query).json())
        self.assertEqual(qs(""), ["Expected CTC", "Notice period", "Why Acme?"])
        self.assertEqual(qs("?scope=company"), ["Why Acme?"])
        self.assertEqual(qs("?source=ai"), ["Expected CTC"])
        self.assertEqual(qs("?q=notice"), ["Notice period"])
        self.assertEqual(qs("?company=acme%20corporation"), ["Why Acme?"])
        self.assertEqual(self.client.get("/api/answer-bank?scope=bogus").status_code, 400)
        self.assertEqual(self.client.get("/api/answer-bank?source=bogus").status_code, 400)

    # ── resolve ───────────────────────────────────────────────────

    def test_resolve_returns_exactly_answer_confidence_and_entry_id(self):
        e = self.post({"question": "What is your expected salary?", "answer": "20 LPA"}).json()
        r = self.resolve("Expected salary")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"answer": "20 LPA", "confidence": 1.0, "entry_id": e["id"]})
        fuzzy = self.resolve("What is your expected salry?").json()
        self.assertEqual((fuzzy["answer"], fuzzy["entry_id"]), ("20 LPA", e["id"]))
        self.assertTrue(0.88 <= fuzzy["confidence"] < 1.0)
        self.assertEqual(self.resolve("Something else entirely").json(),
                         {"answer": None, "confidence": 0.0, "entry_id": None})

    def test_resolve_prefers_the_company_answer_and_does_not_count_a_use(self):
        g = self.post({"question": "Notice period", "answer": "30 days"}).json()
        c = self.post({"question": "Notice period", "answer": "15 days", "scope": "company",
                       "company": "Acme Corp"}).json()
        self.assertEqual(self.resolve("Notice period", "Acme").json()["entry_id"], c["id"])
        self.assertEqual(self.resolve("Notice period", "Globex").json()["entry_id"], g["id"])
        self.assertEqual(self.resolve("Notice period").json()["entry_id"], g["id"])
        self.assertEqual(self.client.get(f"/api/answer-bank/{g['id']}").json()["uses"], 0)

    def test_resolve_needs_a_question(self):
        for body in ({}, {"question": ""}, {"company": "Acme"}, {"question": ["x"]}):
            self.assertEqual(self.client.post("/api/answer-bank/resolve", json=body).status_code, 400, body)
        self.assertEqual(self.resolve("?!").json()["answer"], None)           # nothing meaningful: no match

    # ── isolation ─────────────────────────────────────────────────

    def test_users_cannot_see_or_touch_each_others_answers(self):
        mine = self.post({"question": "Expected CTC", "answer": "20 LPA"}).json()
        self.assertEqual(self.bob.get("/api/answer-bank").json(), [])
        self.assertEqual(self.bob.get(f"/api/answer-bank/{mine['id']}").status_code, 404)
        self.assertEqual(self.bob.put(f"/api/answer-bank/{mine['id']}", json={"answer": "hacked"}).status_code, 404)
        self.assertEqual(self.bob.delete(f"/api/answer-bank/{mine['id']}").status_code, 404)
        self.assertEqual(self.resolve("Expected CTC", client=self.bob).json()["answer"], None)
        self.assertEqual(self.client.get(f"/api/answer-bank/{mine['id']}").json()["answer"], "20 LPA")
        theirs = self.post({"question": "Expected CTC", "answer": "35 LPA"}, self.bob).json()
        self.assertNotEqual(theirs["id"], mine["id"])
        self.assertEqual(self.resolve("Expected CTC").json()["answer"], "20 LPA")
        self.assertEqual(self.resolve("Expected CTC", client=self.bob).json()["answer"], "35 LPA")

    # ── profile entries ───────────────────────────────────────────

    def test_the_profile_owner_sees_profile_facts_read_only(self):
        self.profile(notice_period_label="30 days", expected_ctc_lpa="24")
        rows = {e["id"]: e for e in self.client.get("/api/answer-bank").json()}
        self.assertEqual(rows["profile:notice_period_label"]["answer"], "30 days")
        self.assertEqual((rows["profile:notice_period_label"]["source"], rows["profile:notice_period_label"]["read_only"]),
                         ("profile", True))
        one = self.client.get("/api/answer-bank/profile:expected_ctc_lpa")
        self.assertEqual((one.status_code, one.json()["answer"]), (200, "24"))
        self.assertEqual(self.client.put("/api/answer-bank/profile:expected_ctc_lpa", json={"answer": "99"}).status_code, 403)
        self.assertEqual(self.client.delete("/api/answer-bank/profile:expected_ctc_lpa").status_code, 403)
        self.assertEqual(self.client.get("/api/answer-bank/profile:nope").status_code, 404)
        self.assertEqual(self.client.get("/api/answer-bank?source=profile").json().__len__(), 2)
        r = self.resolve("What is your notice period?").json()
        self.assertEqual((r["answer"], r["confidence"], r["entry_id"]), ("30 days", 1.0, "profile:notice_period_label"))

    def test_the_machines_profile_is_never_shown_to_anyone_but_its_owner(self):
        self.profile(notice_period_label="30 days", expected_ctc_lpa="24")
        self.assertEqual(self.bob.get("/api/answer-bank").json(), [])
        self.assertEqual(self.bob.get("/api/answer-bank/profile:expected_ctc_lpa").status_code, 404)
        self.assertEqual(self.resolve("Expected CTC", client=self.bob).json()["answer"], None)
        self.assertEqual(self.bob.get("/api/answer-bank?source=profile").json(), [])

    def test_your_own_answer_beats_the_profile_fact_through_resolve(self):
        self.profile(notice_period_label="30 days")
        e = self.post({"question": "Notice period", "answer": "15 days"}).json()
        self.assertEqual(self.resolve("Notice period").json()["entry_id"], e["id"])

    def test_the_profile_owner_constant_matches_the_auth_module(self):
        from core.auth import OWNER_USER_ID
        from db.answer_bank_db import PROFILE_OWNER_ID
        self.assertEqual(PROFILE_OWNER_ID, OWNER_USER_ID)

    # ── remember flag on the answer endpoint ──────────────────────

    def test_a_bad_remember_flag_is_refused_without_running_anything(self):
        from applier.session import ApplicationSession
        from db.jobs_db import JobsDB
        db = JobsDB(user_id=1)
        jid = db.add({"url": "https://example.com/1", "title": "Dev", "company": "Acme", "source": "linkedin"})
        db.transition(jid, "preparing"); db.transition(jid, "needs_you", reason="question")
        ApplicationSession(job_id=jid, job_url="u", job_title="t", company="Acme", source="linkedin",
                           state="awaiting_input").save()
        for flag in ("sometimes", {"x": "nope"}, 5):
            r = self.client.post(f"/api/apply/answer/{jid}", json={"answers": {"ctc": "20"}, "remember": flag})
            self.assertEqual(r.status_code, 400, flag)          # and not 409: the job lock was released
        self.assertEqual(db.get_stage(jid), "needs_you")
        self.assertEqual(self.bob.post(f"/api/apply/answer/{jid}", json={"answers": {}}).status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
