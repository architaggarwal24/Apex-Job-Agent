"""
tests/test_sessions.py — revocable sessions: change password, sign out
everywhere, and the single "who is this cookie?" function.

Run from the project root:   python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# core.auth imports fastapi at module level; the token/DB logic under test
# doesn't need it, so stub it if FastAPI isn't installed.
try:
    import fastapi  # noqa: F401
except Exception:
    stub = types.ModuleType("fastapi")
    stub.Request = type("Request", (), {})
    stub.HTTPException = type("HTTPException", (Exception,), {})
    sys.modules["fastapi"] = stub

import core.auth as auth
from core.config import cfg
from db.users_db import UsersDB

PW = "correct-horse-battery"


class SessionVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.users = UsersDB(path=Path(self.tmp.name) / "users.db")
        self._old = (cfg.APP_SECRET_KEY, auth._serializer)
        cfg.APP_SECRET_KEY, auth._serializer = "s" * 48, None
        self.u = self.users.create_user("a@b.co", PW)

    def tearDown(self):
        cfg.APP_SECRET_KEY, auth._serializer = self._old
        self.tmp.cleanup()

    def token(self, version=0, uid=None):
        return auth.create_session_token(uid or self.u["id"], version)

    def who(self, token):
        return auth.user_for_token(token, users=self.users)

    # ── the token ─────────────────────────────────────────────────

    def test_token_roundtrips_user_and_version(self):
        self.assertEqual(auth.read_session_token(self.token(3)), (self.u["id"], 3))
        self.assertEqual(auth.verify_session_token(self.token(3)), self.u["id"])

    def test_tokens_from_before_versions_existed_still_work_as_version_zero(self):
        legacy = auth._get_serializer().dumps({"uid": self.u["id"]})         # the old payload shape
        self.assertEqual(auth.read_session_token(legacy), (self.u["id"], 0))
        self.assertEqual(self.who(legacy)["id"], self.u["id"], "upgrading must not log everyone out")

    def test_malformed_payloads_are_rejected(self):
        ser = auth._get_serializer()
        for payload in [{"uid": "1"}, {"uid": True}, {"uid": None}, {}, [1], "x", {"sv": 1}]:
            self.assertIsNone(auth.read_session_token(ser.dumps(payload)), payload)
        self.assertIsNone(auth.read_session_token("garbage"))
        self.assertIsNone(auth.read_session_token(self.token()[:-3] + "xxx"))
        # a non-int version degrades to 0 rather than crashing
        self.assertEqual(auth.read_session_token(ser.dumps({"uid": 1, "sv": "9"})), (1, 0))

    # ── user_for_token: the one decision point ────────────────────

    def test_valid_token_resolves_to_the_user_without_leaking_the_password_hash(self):
        user = self.who(self.token())
        self.assertEqual(user["email"], "a@b.co")
        self.assertNotIn("password_hash", user)

    def test_no_token_bad_token_unknown_user_are_all_none(self):
        self.assertIsNone(self.who(None)); self.assertIsNone(self.who(""))
        self.assertIsNone(self.who("garbage"))
        self.assertIsNone(self.who(self.token(uid=999)))

    def test_deactivated_account_is_refused(self):
        with self.users._conn() as c:
            c.execute("UPDATE users SET is_active=0")
        self.assertIsNone(self.who(self.token()))

    def test_stale_version_is_refused_and_current_version_accepted(self):
        old = self.token(0)
        new_version = self.users.bump_session_version(self.u["id"])
        self.assertEqual(new_version, 1)
        self.assertIsNone(self.who(old), "a revoked cookie must stop working")
        self.assertIsNotNone(self.who(self.token(new_version)))
        self.assertIsNone(self.who(self.token(new_version + 1)), "a FUTURE version must not be accepted either")

    # ── change password ───────────────────────────────────────────

    def test_change_password_revokes_old_sessions_and_swaps_the_credential(self):
        old = self.token(0)
        v = self.users.change_password(self.u["id"], PW, "a-brand-new-password")
        self.assertEqual(v, 1)
        self.assertIsNone(self.who(old))
        self.assertIsNotNone(self.who(self.token(v)))
        self.assertIsNone(self.users.verify_login("a@b.co", PW), "old password still works")
        self.assertIsNotNone(self.users.verify_login("a@b.co", "a-brand-new-password"))

    def test_login_returns_the_current_version_so_the_new_cookie_is_valid(self):
        self.users.bump_session_version(self.u["id"]); self.users.bump_session_version(self.u["id"])
        login = self.users.verify_login("a@b.co", PW)
        self.assertEqual(login["session_version"], 2)
        self.assertIsNotNone(self.who(self.token(login["session_version"])))

    def test_change_password_rejections_change_nothing(self):
        uid = self.u["id"]
        with self.assertRaises(PermissionError):
            self.users.change_password(uid, "wrong-current-pw", "another-good-password")
        with self.assertRaises(ValueError):
            self.users.change_password(uid, PW, "short")
        with self.assertRaises(ValueError):
            self.users.change_password(uid, PW, PW)
        with self.assertRaises(LookupError):
            self.users.change_password(999, PW, "another-good-password")
        self.assertIsNotNone(self.users.verify_login("a@b.co", PW))
        self.assertEqual(self.users.get_by_id(uid)["session_version"], 0, "a failed change bumped the version")

    def test_one_users_change_does_not_log_out_another(self):
        other = self.users.create_user("c@d.co", PW)
        self.users.change_password(self.u["id"], PW, "a-brand-new-password")
        self.assertIsNotNone(self.who(self.token(0, uid=other["id"])))

    def test_existing_database_without_the_column_is_upgraded_in_place(self):
        import sqlite3
        path = Path(self.tmp.name) / "legacy.db"
        con = sqlite3.connect(path)
        con.execute("""CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
                       password_hash TEXT NOT NULL, created_at TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1)""")
        from db.users_db import _hash_password
        con.execute("INSERT INTO users (email,password_hash,created_at) VALUES (?,?,?)",
                    ("old@x.co", _hash_password(PW), "2026-01-01"))
        con.commit(); con.close()
        db = UsersDB(path=path)
        self.assertEqual(db.verify_login("old@x.co", PW)["session_version"], 0)
        UsersDB(path=path)                                   # re-open: ALTER must be idempotent


if __name__ == "__main__":
    unittest.main(verbosity=2)
