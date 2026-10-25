"""
tests/test_http_auth.py — end-to-end tests of the auth gate, signup
policy, per-user job isolation over HTTP, and the WebSocket check.

Run from the project root:   python3 -m unittest discover -s tests -v

NOTE: written without the ability to execute it (the authoring sandbox
had no FastAPI). It skips itself when FastAPI isn't installed and runs
for real once `pip install -r requirements.txt` has been done. If a test
here fails, treat it as a real finding first and a test bug second —
these check exactly the behaviours the security model depends on.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Never let a test create or read the real data/.master.key: give this
# whole test process its own throwaway encryption key up front.
from cryptography.fernet import Fernet
os.environ["APP_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:           # fastapi/httpx not installed
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class HttpAuthTests(unittest.TestCase):

    def setUp(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB

        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        # Settings tests write settings — never into the real data/settings.db.
        self._saved_settings = SettingsDB._instance
        SettingsDB._instance = SettingsDB(path=tmp / "settings.db")

        # Redirect every piece of persistent state into a temp dir so the
        # tests never touch (or depend on) real user data.
        self._saved = (cfg.JOBS_DB_PATH, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE,
                       cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer)
        cfg.JOBS_DB_PATH   = tmp / "jobs.db"
        cfg.APP_SECRET_KEY = "test-signing-secret-" + "x" * 40   # must be >= 32 chars,
                                                              # or auth ignores it and
                                                              # writes a real key file
        cfg.COOKIE_SECURE  = False     # TestClient is plain http; a Secure
                                       # cookie would never be sent back
        cfg.ALLOW_SIGNUP   = False
        UsersDB._instance  = UsersDB(path=tmp / "users.db")
        auth._serializer   = None      # force re-init with the test secret

        # Deterministic regardless of whether the developer has built the UI:
        # point at a directory that doesn't exist and make sure the dev-server
        # redirect isn't switched on by a leftover env var.
        self._saved_dev_origin = os.environ.pop("APEX_DEV_UI_ORIGIN", None)
        self.tmp_path = tmp
        from server.app import create_app
        self.app    = create_app(ui_dir=tmp / "no-build")
        self.client = TestClient(self.app, follow_redirects=False)

    def tearDown(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB
        SettingsDB._instance = self._saved_settings
        (cfg.JOBS_DB_PATH, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE,
         cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer) = self._saved
        os.environ.pop("APEX_DEV_UI_ORIGIN", None)
        if self._saved_dev_origin is not None:
            os.environ["APEX_DEV_UI_ORIGIN"] = self._saved_dev_origin
        self.tmp.cleanup()

    def _signup(self, client, email, pw="correct-horse-battery"):
        return client.post("/api/auth/signup", json={"email": email, "password": pw})

    def _second_client(self):
        return TestClient(self.app, follow_redirects=False)

    # ── the gate ──────────────────────────────────────────────────

    def test_anonymous_api_request_is_401(self):
        self.assertEqual(self.client.get("/api/applications").status_code, 401)
        self.assertEqual(self.client.get("/api/stats").status_code, 401)

    def test_anonymous_request_to_a_protected_page_goes_to_the_app_root(self):
        # The login screen is part of the Next app, served at "/".
        for path in ("/docs", "/openapi.json", "/no-such-page"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 303, path)
            self.assertEqual(r.headers["location"], "/", path)

    def test_old_page_urls_redirect_to_the_pages_that_replaced_them(self):
        # Public on purpose: a logged-out bookmark lands on the right page, which
        # then shows the login screen. Query strings survive the hop.
        want = {"/apply": "/applications/", "/tracking": "/applications/",
                "/jobs": "/applications/", "/setup": "/"}
        self._signup(self._second_client(), "owner@example.com")
        signed_in = self._second_client()
        signed_in.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct-horse-battery"})
        for c in (self.client, signed_in):
            for old, new in want.items():
                for path in (old, old + "/"):
                    r = c.get(path)
                    self.assertEqual((r.status_code, r.headers.get("location")), (307, new), path)
                r = c.get(old + "?stage=needs_you&q=a%20b")
                self.assertEqual(r.headers["location"], new + "?stage=needs_you&q=a%20b", old)
        self.assertEqual(self.client.post("/apply").status_code, 405)      # only GET is a redirect

    def test_old_login_url_redirects_to_the_app_and_auth_endpoints_are_public(self):
        r = self.client.get("/login")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/"))
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

    # ── serving the built Next.js export ──────────────────────────

    def _client_with_build(self):
        from server.app import create_app
        out = self.tmp_path / "out"
        (out / "applications").mkdir(parents=True)
        (out / "_next" / "static").mkdir(parents=True)
        (out / "index.html").write_text("SHELL-HOME")
        (out / "applications" / "index.html").write_text("SHELL-APPS")
        (out / "_next" / "static" / "app.js").write_text("console.log(1)")
        (self.tmp_path / "secret.txt").write_text("TOP-SECRET")
        return TestClient(create_app(ui_dir=out), follow_redirects=False)

    def test_built_app_shell_loads_for_logged_out_visitors_but_nothing_else_does(self):
        c = self._client_with_build()
        for path, marker in (("/", "SHELL-HOME"), ("/applications/", "SHELL-APPS"),
                             ("/_next/static/app.js", "console.log")):
            r = c.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn(marker, r.text, path)
        self.assertEqual(c.get("/api/applications").status_code, 401)
        self.assertEqual(c.get("/api/settings").status_code, 401)
        self.assertEqual((c.get("/jobs/").status_code, c.get("/jobs/").headers["location"]), (307, "/applications/"))
        self.assertEqual(c.get("/docs").status_code, 303)

    def test_static_mount_cannot_be_used_to_read_files_outside_the_build(self):
        c = self._client_with_build()
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/applications/../../secret.txt",
                     "/%2e%2e%2fsecret.txt", "/_next/../../secret.txt"):
            r = c.get(path)
            self.assertNotIn("TOP-SECRET", r.text, path)
            self.assertNotEqual(r.status_code, 200, path)

    def test_api_routes_still_win_over_the_catch_all_static_mount(self):
        c = self._client_with_build()
        self._signup(c, "owner@example.com")
        self.assertEqual(c.get("/api/applications").status_code, 200)     # JSON, not index.html
        self.assertIsInstance(c.get("/api/applications").json(), list)

    def test_without_a_build_the_dev_server_redirect_can_be_enabled(self):
        from server.app import create_app
        os.environ["APEX_DEV_UI_ORIGIN"] = "http://localhost:3000"
        c = TestClient(create_app(ui_dir=self.tmp_path / "no-build"), follow_redirects=False)
        for path in ("/", "/applications", "/profile", "/settings"):
            r = c.get(path)        # anonymous too — must not bounce into a loop
            self.assertEqual(r.status_code, 307, path)
            self.assertEqual(r.headers["location"], "http://localhost:3000" + path, path)
        self.assertEqual(c.get("/apply").headers["location"], "/applications/")   # old URLs still redirect

    def test_screenshots_require_login(self):
        # Filled-in application forms — must never be world-readable.
        r = self.client.get("/screenshots/anything.png")
        self.assertIn(r.status_code, (303, 401))

    def test_forged_cookie_is_rejected(self):
        self.client.cookies.set("apex_session", "forged.value.here")
        self.assertEqual(self.client.get("/api/applications").status_code, 401)

    # ── signup policy ─────────────────────────────────────────────

    def test_first_signup_allowed_second_blocked_by_default(self):
        self.assertEqual(self._signup(self.client, "owner@example.com").status_code, 200)
        r = self._signup(self._second_client(), "other@example.com")
        self.assertEqual(r.status_code, 403)

    def test_signup_can_be_opened_explicitly(self):
        from core.config import cfg
        self._signup(self.client, "owner@example.com")
        cfg.ALLOW_SIGNUP = True
        r = self._signup(self._second_client(), "other@example.com")
        self.assertEqual(r.status_code, 200)

    def test_signup_validation(self):
        self.assertEqual(self._signup(self.client, "not-an-email").status_code, 400)
        self.assertEqual(self._signup(self.client, "a@b.co", pw="short").status_code, 400)

    # ── login / logout ────────────────────────────────────────────

    def test_login_wrong_password_and_unknown_user_look_identical(self):
        self._signup(self.client, "owner@example.com")
        bad_pw  = self._second_client().post("/api/auth/login",
                    json={"email": "owner@example.com", "password": "wrong-password"})
        no_user = self._second_client().post("/api/auth/login",
                    json={"email": "ghost@example.com", "password": "wrong-password"})
        self.assertEqual(bad_pw.status_code, 401)
        self.assertEqual(no_user.status_code, 401)
        self.assertEqual(bad_pw.json(), no_user.json(),
                         "different errors let an attacker enumerate accounts")

    def test_repeated_bad_logins_get_throttled_then_blocked_even_if_correct(self):
        from core.throttle import login_throttle
        login_throttle._fails.clear()
        self._signup(self.client, "owner@example.com")
        c = self._second_client()
        for _ in range(5):
            r = c.post("/api/auth/login",
                       json={"email": "owner@example.com", "password": "wrong-password"})
            self.assertEqual(r.status_code, 401)
        blocked = c.post("/api/auth/login",
                         json={"email": "owner@example.com",
                               "password": "correct-horse-battery"})
        self.assertEqual(blocked.status_code, 429,
                         "even the CORRECT password must be refused while locked out")
        self.assertIn("retry-after", {k.lower() for k in blocked.headers})
        login_throttle._fails.clear()

    def test_session_works_then_logout_ends_it(self):
        self._signup(self.client, "owner@example.com")
        self.assertEqual(self.client.get("/api/applications").status_code, 200)
        self.assertTrue(self.client.get("/api/auth/me").json()["authenticated"])

        self.client.post("/api/auth/logout")
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/applications").status_code, 401)

    def test_deactivated_account_session_dies_immediately(self):
        from db.users_db import UsersDB
        self._signup(self.client, "owner@example.com")
        self.assertEqual(self.client.get("/api/applications").status_code, 200)
        with UsersDB.get()._conn() as c:
            c.execute("UPDATE users SET is_active=0")
        self.assertEqual(self.client.get("/api/applications").status_code, 401,
                         "valid-signature token outlived account deactivation")

    # ── per-user data isolation over HTTP ─────────────────────────

    def test_users_only_see_their_own_jobs(self):
        from core.config import cfg
        from db.jobs_db import JobsDB
        self._signup(self.client, "alice@example.com")            # user 1
        cfg.ALLOW_SIGNUP = True
        bob = self._second_client()
        self._signup(bob, "bob@example.com")                      # user 2

        a = JobsDB(user_id=1).add({"url": "https://x.com/a", "title": "Alice Job",
                                   "company": "A", "source": "linkedin"})
        b = JobsDB(user_id=2).add({"url": "https://x.com/b", "title": "Bob Job",
                                   "company": "B", "source": "linkedin"})

        self.assertEqual([j["title"] for j in self.client.get("/api/applications").json()],
                         ["Alice Job"])
        self.assertEqual([j["title"] for j in bob.get("/api/applications").json()],
                         ["Bob Job"])
        self.assertEqual(bob.get(f"/api/jobs/{a}").status_code, 404,
                         "Bob fetched Alice's job by ID")
        self.assertEqual(self.client.get(f"/api/jobs/{b}").status_code, 404)

    def test_clear_all_over_http_only_clears_callers_jobs(self):
        from core.config import cfg
        from db.jobs_db import JobsDB
        self._signup(self.client, "alice@example.com")
        cfg.ALLOW_SIGNUP = True
        bob = self._second_client()
        self._signup(bob, "bob@example.com")
        JobsDB(user_id=1).add({"url": "https://x.com/a", "title": "A",
                               "company": "A", "source": "linkedin"})
        JobsDB(user_id=2).add({"url": "https://x.com/b", "title": "B",
                               "company": "B", "source": "linkedin"})

        self.assertEqual(self.client.delete("/api/jobs").status_code, 200)
        self.assertEqual(len(JobsDB(user_id=2).get_all()), 1)

    # ── shared-browser actions are owner-only for now ─────────────

    def test_non_owner_cannot_run_or_cancel_pipeline(self):
        from core.config import cfg
        self._signup(self.client, "owner@example.com")
        cfg.ALLOW_SIGNUP = True
        bob = self._second_client()
        self._signup(bob, "bob@example.com")

        self.assertEqual(bob.post("/api/pipeline/run", json={}).status_code, 403)
        self.assertEqual(bob.post("/api/pipeline/cancel").status_code, 403)

    # ── settings API ──────────────────────────────────────────────

    def _owner(self):
        self._signup(self.client, "owner@example.com")
        return self.client

    def _second_user(self):
        from core.config import cfg
        cfg.ALLOW_SIGNUP = True
        bob = self._second_client()
        self._signup(bob, "bob@example.com")
        return bob

    def test_settings_writes_are_owner_only_but_reads_are_not(self):
        self._owner(); bob = self._second_user()
        for path, body in [("/api/settings", {"llm_temperature": "0.2"}),
                           ("/api/settings/custom-providers", {"label": "X", "base_url": "https://x.example.com/v1"}),
                           ("/api/settings/models", {"provider": "openai", "model": "m"}),
                           ("/api/settings/models/remove", {"provider": "openai", "model": "m"}),
                           ("/api/settings/test-llm", {}),
                           ("/api/settings/fetch-models", {"provider": "openai"}),
                           ("/api/settings/clear-cookies/linkedin", None)]:
            r = bob.post(path, json=body) if body is not None else bob.post(path)
            self.assertEqual(r.status_code, 403, path)
        self.assertEqual(bob.get("/api/settings/providers").status_code, 200)

    def test_save_validates_everything_before_writing_anything(self):
        c = self._owner()
        before = c.get("/api/settings").json()["settings"]
        bad_bodies = [
            {"llm_temperature": "0.9", "openai_base_url": "http://169.254.169.254/latest"},   # SSRF, mixed with a fine key
            {"nvidia_base_url": "file:///etc/passwd"},
            {"custom_providers": "[]"},                       # has its own validated endpoint
            {"extra_models_openai": "[]"},
            {"openai_model": "not a valid model!"},
            {"llm_provider": "no-such-provider"},
            {"gemini_api_key": "your_gemini_key_here"},       # placeholder text, not a key
            {"openai_model": {"nested": "object"}},
        ]
        for body in bad_bodies:
            self.assertEqual(c.post("/api/settings", json=body).status_code, 400, body)
        after = c.get("/api/settings").json()["settings"]
        self.assertEqual(before, after, "a rejected save wrote something anyway")

    def test_save_accepts_normal_changes_and_masks_keys_on_read(self):
        c = self._owner()
        r = c.post("/api/settings", json={"llm_temperature": "0.3", "openai_api_key": "sk-live-abcdef1234",
                                           "ollama_base_url": "http://localhost:11434/v1/"})
        self.assertEqual((r.status_code, r.json()["saved"]), (200, 3))
        got = c.get("/api/settings").json()["settings"]
        self.assertEqual(got["llm_temperature"], "0.3")
        self.assertEqual(got["ollama_base_url"], "http://localhost:11434/v1")           # normalised
        self.assertTrue(got["openai_api_key"].startswith("••••")); self.assertNotIn("abcdef", got["openai_api_key"])
        # a masked value sent back must NOT overwrite the real key
        c.post("/api/settings", json={"openai_api_key": got["openai_api_key"]})
        from db.settings_db import SettingsDB
        self.assertEqual(SettingsDB.get().read("openai_api_key"), "sk-live-abcdef1234")

    def test_custom_provider_lifecycle(self):
        c = self._owner()
        r = c.post("/api/settings/custom-providers", json={
            "label": "CodeCraft API", "base_url": "https://codecraftapi.com/v1",
            "api_key": "cc_secret_value_123", "models": ["claude-opus-4.8"]})
        self.assertEqual(r.status_code, 200); pid = r.json()["id"]
        self.assertEqual(pid, "custom-codecraft-api")

        provs = c.get("/api/settings/providers").json()
        self.assertTrue(provs[pid]["custom"]); self.assertTrue(provs[pid]["configured"])
        self.assertEqual(provs[pid]["extra_models"], ["claude-opus-4.8"])
        self.assertNotIn("cc_secret_value_123", c.get("/api/settings/providers").text)
        self.assertNotIn("cc_secret_value_123", c.get("/api/settings").text, "key must be masked on read")

        self.assertEqual(c.post("/api/settings", json={"llm_provider": pid, "custom_codecraft-api_model": "claude-opus-4.8"}).status_code, 200)
        self.assertEqual(c.get("/api/settings").json()["provider_info"]["provider"], pid)

        self.assertEqual(c.patch(f"/api/settings/custom-providers/{pid}", json={"label": "CC"}).status_code, 200)
        self.assertEqual(c.patch(f"/api/settings/custom-providers/{pid}",
                                 json={"base_url": "http://169.254.169.254/"}).status_code, 400)
        self.assertEqual(c.patch("/api/settings/custom-providers/custom-nope", json={"label": "x"}).status_code, 404)

        d = c.delete(f"/api/settings/custom-providers/{pid}")
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.json()["provider_info"]["provider"], "nvidia", "active provider not reset")
        self.assertNotIn(pid, c.get("/api/settings/providers").json())
        self.assertEqual(c.delete(f"/api/settings/custom-providers/{pid}").status_code, 404)

    def test_custom_provider_rejects_unsafe_input(self):
        c = self._owner()
        for body in [{"label": "x", "base_url": "http://169.254.169.254/latest"},
                     {"label": "x", "base_url": "http://2852039166/"},
                     {"label": "x", "base_url": "ftp://x.com"}, {"label": "", "base_url": "https://x.com/v1"},
                     {"label": "x", "base_url": "https://x.com/v1", "models": ["bad model"]}]:
            self.assertEqual(c.post("/api/settings/custom-providers", json=body).status_code, 400, body)
        self.assertEqual(c.get("/api/settings/providers").json().keys() & {"custom-x"}, set())

    def test_added_models_are_remembered_per_provider(self):
        c = self._owner()
        self.assertEqual(c.post("/api/settings/models", json={"provider": "openai", "model": "my-ft-1"}).json(),
                         {"extra_models": ["my-ft-1"]})
        self.assertEqual(c.get("/api/settings/providers").json()["openai"]["extra_models"], ["my-ft-1"])
        self.assertEqual(c.get("/api/settings/providers").json()["anthropic"]["extra_models"], [])
        self.assertEqual(c.post("/api/settings/models", json={"provider": "openai", "model": "bad name"}).status_code, 400)
        self.assertEqual(c.post("/api/settings/models", json={"provider": "nope", "model": "m"}).status_code, 404)
        self.assertEqual(c.post("/api/settings/models/remove", json={"provider": "openai", "model": "my-ft-1"}).json(),
                         {"extra_models": []})

    def test_test_connection_uses_the_values_on_screen_not_the_saved_ones(self):
        c = self._owner()
        captured = {}
        def fake(cfg):
            captured.update(cfg); return {"response": "OK", "latency_ms": 12, "note": ""}
        with mock.patch("core.llm.test_connection", side_effect=fake):
            r = c.post("/api/settings/test-llm", json={
                "provider": "openai", "model": "my-model", "api_key": "sk-typed-but-unsaved",
                "base_url": "https://example.com/v1/"})
        self.assertEqual((r.status_code, r.json()["response"], r.json()["provider"]), (200, "OK", "openai"))
        self.assertEqual((captured["model"], captured["api_key"], captured["base_url"]),
                         ("my-model", "sk-typed-but-unsaved", "https://example.com/v1"))
        from db.settings_db import SettingsDB
        self.assertEqual(SettingsDB.get().read("openai_api_key"), "", "testing must not save anything")

    def test_a_masked_or_blank_key_in_a_test_means_use_the_saved_one(self):
        c = self._owner()
        c.post("/api/settings", json={"openai_api_key": "sk-saved-key-9999"})
        for sent in ("", "••••••••9999", None):
            captured = {}
            with mock.patch("core.llm.test_connection", side_effect=lambda cfg: (captured.update(cfg),
                            {"response": "OK", "latency_ms": 1, "note": ""})[1]):
                c.post("/api/settings/test-llm", json={"provider": "openai", "api_key": sent})
            self.assertEqual(captured["api_key"], "sk-saved-key-9999", repr(sent))

    def test_test_connection_failures_come_back_as_a_readable_400(self):
        from core.llm import LLMTestError
        c = self._owner()
        with mock.patch("core.llm.test_connection", side_effect=LLMTestError("The provider rejected the API key (401).")):
            r = c.post("/api/settings/test-llm", json={"provider": "openai"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("rejected the API key", r.json()["detail"])      # `detail`: what the web app displays
        self.assertEqual(c.post("/api/settings/test-llm", json={"provider": "nope"}).status_code, 404)
        self.assertEqual(c.post("/api/settings/test-llm", json={"provider": "openai",
                                "base_url": "http://169.254.169.254/"}).status_code, 400)

    def test_fetch_models(self):
        c = self._owner()
        self.assertEqual(c.post("/api/settings/fetch-models", json={"provider": "openai"}).status_code, 400,
                         "no key yet — should say so, not call out")
        with mock.patch("core.llm.list_models", return_value=["a", "b"]) as lm:
            r = c.post("/api/settings/fetch-models", json={"provider": "openai", "api_key": "sk-typed-123456"})
        self.assertEqual(r.json(), {"models": ["a", "b"]})
        self.assertEqual(lm.call_args.args[0]["api_key"], "sk-typed-123456")

    # ── change password / sign out everywhere ─────────────────────

    def _two_browsers(self):
        """Same account, two independent cookie jars (e.g. laptop + phone)."""
        self._signup(self.client, "owner@example.com")
        phone = self._second_client()
        r = phone.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200)
        return self.client, phone

    def test_change_password_signs_out_other_devices_but_not_this_one(self):
        laptop, phone = self._two_browsers()
        self.assertEqual(phone.get("/api/applications").status_code, 200)
        r = laptop.post("/api/auth/change-password", json={
            "current_password": "correct-horse-battery", "new_password": "a-brand-new-password"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(laptop.get("/api/applications").status_code, 200, "this browser must stay signed in")
        self.assertEqual(phone.get("/api/applications").status_code, 401, "the other device must be signed out")
        self.assertFalse(phone.get("/api/auth/me").json()["authenticated"],
                         "/me must agree with the gate or the app reload-loops")
        fresh = self._second_client()
        self.assertEqual(fresh.post("/api/auth/login", json={"email": "owner@example.com",
                         "password": "correct-horse-battery"}).status_code, 401)
        self.assertEqual(fresh.post("/api/auth/login", json={"email": "owner@example.com",
                         "password": "a-brand-new-password"}).status_code, 200)

    def test_change_password_errors_use_400_and_403_never_401(self):
        from core.throttle import login_throttle
        login_throttle._fails.clear()
        laptop, _ = self._two_browsers()
        wrong = laptop.post("/api/auth/change-password", json={"current_password": "nope-nope-nope", "new_password": "a-brand-new-password"})
        short = laptop.post("/api/auth/change-password", json={"current_password": "correct-horse-battery", "new_password": "short"})
        same  = laptop.post("/api/auth/change-password", json={"current_password": "correct-horse-battery", "new_password": "correct-horse-battery"})
        self.assertEqual((wrong.status_code, short.status_code, same.status_code), (403, 400, 400),
                         "a 401 here would make the web app think the session expired and reload")
        self.assertEqual(laptop.get("/api/applications").status_code, 200, "failed attempts must not sign you out")
        login_throttle._fails.clear()

    def test_wrong_current_password_is_throttled_like_a_login(self):
        from core.throttle import login_throttle
        login_throttle._fails.clear()
        laptop, _ = self._two_browsers()
        codes = [laptop.post("/api/auth/change-password", json={"current_password": f"wrong-guess-{i}",
                 "new_password": "a-brand-new-password"}).status_code for i in range(6)]
        self.assertEqual(codes[:5], [403] * 5); self.assertEqual(codes[5], 429)
        login_throttle._fails.clear()

    def test_logout_everywhere(self):
        laptop, phone = self._two_browsers()
        self.assertEqual(laptop.post("/api/auth/logout-everywhere").status_code, 200)
        self.assertEqual(laptop.get("/api/applications").status_code, 200)
        self.assertEqual(phone.get("/api/applications").status_code, 401)
        self.assertFalse(phone.get("/api/auth/me").json()["authenticated"])

    def test_change_password_and_logout_everywhere_require_a_session(self):
        for path, body in [("/api/auth/change-password", {"current_password": "x", "new_password": "y" * 9}),
                           ("/api/auth/logout-everywhere", None)]:
            r = self.client.post(path, json=body) if body else self.client.post(path)
            self.assertEqual(r.status_code, 401, path)

    # ── websocket ─────────────────────────────────────────────────

    def test_websocket_rejects_anonymous_and_non_owners(self):
        from core.config import cfg
        from db.jobs_db import JobsDB
        from starlette.websockets import WebSocketDisconnect

        self._signup(self.client, "alice@example.com")
        cfg.ALLOW_SIGNUP = True
        bob = self._second_client()
        self._signup(bob, "bob@example.com")
        a = JobsDB(user_id=1).add({"url": "https://x.com/a", "title": "A",
                                   "company": "A", "source": "linkedin"})

        anon = self._second_client()
        with self.assertRaises(WebSocketDisconnect):
            with anon.websocket_connect(f"/ws/{a}"):
                pass
        with self.assertRaises(WebSocketDisconnect):
            with bob.websocket_connect(f"/ws/{a}"):
                pass                       # logged in, but not Alice's job


if __name__ == "__main__":
    unittest.main(verbosity=2)
