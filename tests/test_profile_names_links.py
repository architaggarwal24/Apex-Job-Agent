# tests/test_profile_names_links.py
"""
tests/test_profile_names_links.py — the Profile page asks for one full name and a
user name for GitHub / LinkedIn:

  * split_full_name / canonical_profile_url / profile_url_username (pure helpers)
  * GET /api/profile hides first_name / last_name; /api/profile/flat still has them
  * POST /api/profile derives the halves from full_name, refuses first_name /
    last_name as inputs, and stores GitHub / LinkedIn as canonical URLs whatever
    shape was sent
  * import_from_parsed canonicalises the parser's links and splits the name

Run from the project root:   python3 -m unittest tests.test_profile_names_links -v
No LLM or network: the parser output is a plain dict.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from db.profile_db import canonical_profile_url, profile_url_username, split_full_name

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


class HelperTests(unittest.TestCase):

    def test_full_name_splits_into_first_and_the_rest(self):
        self.assertEqual(split_full_name("Archit Aggarwal"), ("Archit", "Aggarwal"))
        self.assertEqual(split_full_name("  Mary Jane  Watson "), ("Mary", "Jane Watson"))
        self.assertEqual(split_full_name("Cher"), ("Cher", ""))
        self.assertEqual(split_full_name(""), ("", ""))

    def test_every_spelling_of_a_github_link_gives_the_same_url(self):
        for raw in ("architaggarwal24", "@architaggarwal24", "github.com/architaggarwal24",
                    "https://www.github.com/architaggarwal24/", "http://GitHub.com/architaggarwal24?tab=repos"):
            self.assertEqual(canonical_profile_url("github_url", raw), "https://github.com/architaggarwal24", raw)
        self.assertEqual(profile_url_username("github_url", "https://github.com/architaggarwal24/apex"), "architaggarwal24")

    def test_every_spelling_of_a_linkedin_link_gives_the_same_url(self):
        for raw in ("architaggarwal24", "linkedin.com/in/architaggarwal24",
                    "https://in.linkedin.com/in/architaggarwal24/", "https://www.linkedin.com/pub/architaggarwal24"):
            self.assertEqual(canonical_profile_url("linkedin_url", raw),
                             "https://www.linkedin.com/in/architaggarwal24", raw)

    def test_an_empty_link_stays_empty(self):
        self.assertEqual(canonical_profile_url("github_url", ""), "")
        self.assertEqual(canonical_profile_url("linkedin_url", "   /  "), "")


class ImportFromParsedTests(unittest.TestCase):

    def setUp(self):
        from core.config import cfg
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = cfg.PROFILE_DB_PATH
        cfg.PROFILE_DB_PATH = Path(self.tmp.name) / "profile.db"

    def tearDown(self):
        from core.config import cfg
        cfg.PROFILE_DB_PATH = self._saved
        self.tmp.cleanup()

    def test_parser_output_is_stored_split_and_canonical(self):
        from db.profile_db import ProfileDB
        db = ProfileDB()
        db.import_from_parsed({"full_name": "Archit Aggarwal", "first_name": "Wrong", "last_name": "Half",
                               "github_url": "github.com/architaggarwal24",
                               "linkedin_url": "https://in.linkedin.com/in/architaggarwal24/"})
        flat = db.get_all()
        self.assertEqual((flat["first_name"], flat["last_name"]), ("Archit", "Aggarwal"))
        self.assertEqual(flat["github_url"], "https://github.com/architaggarwal24")
        self.assertEqual(flat["linkedin_url"], "https://www.linkedin.com/in/architaggarwal24")


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class ProfileHttpTests(unittest.TestCase):

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
        self.client = TestClient(create_app(ui_dir=tmp / "no-build"), follow_redirects=False)
        self.client.post("/api/auth/signup",
                         json={"email": "alice@example.com", "password": "correct-horse-battery"})

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

    def _keys(self, by_cat):
        return {f["key"] for fields in by_cat.values() for f in fields}

    def test_the_page_sees_one_name_field_and_the_form_filler_sees_three(self):
        keys = self._keys(self.client.get("/api/profile").json())
        self.assertIn("full_name", keys)
        self.assertNotIn("first_name", keys)
        self.assertNotIn("last_name", keys)
        flat = self.client.get("/api/profile/flat").json()
        self.assertIn("first_name", flat)
        self.assertIn("last_name", flat)

    def test_the_page_gets_fields_in_the_profile_order(self):
        from db.profile_db import STANDARD_FIELDS
        page = self.client.get("/api/profile").json()
        want = [k for k, _d, cat, _t, _l in STANDARD_FIELDS if cat == "personal" and k not in ("first_name", "last_name")]
        self.assertEqual([f["key"] for f in page["personal"]], want)
        self.assertEqual(page["personal"][0]["key"], "full_name")

    def test_saving_the_full_name_fills_the_halves(self):
        r = self.client.post("/api/profile", json={"full_name": "Archit Aggarwal"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["updated"], 1)          # the halves are not counted as edits
        flat = self.client.get("/api/profile/flat").json()
        self.assertEqual((flat["full_name"], flat["first_name"], flat["last_name"]),
                         ("Archit Aggarwal", "Archit", "Aggarwal"))

    def test_the_halves_cannot_be_set_on_their_own(self):
        self.client.post("/api/profile", json={"full_name": "Archit Aggarwal"})
        r = self.client.post("/api/profile", json={"first_name": "Someone", "last_name": "Else"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(r.json()["rejected"]), ["first_name", "last_name"])
        flat = self.client.get("/api/profile/flat").json()
        self.assertEqual((flat["first_name"], flat["last_name"]), ("Archit", "Aggarwal"))

    def test_links_are_stored_canonical_from_a_user_name_or_a_full_url(self):
        self.client.post("/api/profile", json={"github_url": "architaggarwal24",
                                               "linkedin_url": "https://www.linkedin.com/in/architaggarwal24/"})
        flat = self.client.get("/api/profile/flat").json()
        self.assertEqual(flat["github_url"], "https://github.com/architaggarwal24")
        self.assertEqual(flat["linkedin_url"], "https://www.linkedin.com/in/architaggarwal24")
        self.client.post("/api/profile", json={"github_url": ""})
        self.assertEqual(self.client.get("/api/profile/flat").json()["github_url"], "")


if __name__ == "__main__":
    unittest.main()
