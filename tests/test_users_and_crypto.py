"""
tests/test_users_and_crypto.py — password hashing, account rules, and
encryption-at-rest behaviour.

Run from the project root:   python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet

from db.users_db import UsersDB, _hash_password, _verify_password


class PasswordHashingTests(unittest.TestCase):
    def test_hash_is_salted_and_never_contains_plaintext(self):
        h1, h2 = _hash_password("hunter2hunter2"), _hash_password("hunter2hunter2")
        self.assertNotEqual(h1, h2, "same password must hash differently (salt)")
        self.assertNotIn("hunter2", h1)
        self.assertTrue(h1.startswith("scrypt:"))

    def test_verify(self):
        h = _hash_password("correct-horse-battery")
        self.assertTrue(_verify_password("correct-horse-battery", h))
        self.assertFalse(_verify_password("correct-horse-batterY", h))
        self.assertFalse(_verify_password("", h))

    def test_malformed_stored_hash_fails_closed(self):
        for bad in ("", "garbage", "scrypt:1:2", "md5:16384:8:1:aa:bb",
                    "scrypt:16384:8:1:zz:zz"):
            self.assertFalse(_verify_password("anything", bad), bad)


class UsersDBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = UsersDB(path=Path(self.tmp.name) / "users.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_signup_login_roundtrip_and_email_normalised(self):
        u = self.db.create_user("  Test@Example.COM ", "correct-horse-battery")
        self.assertEqual(u["email"], "test@example.com")
        self.assertEqual(
            self.db.verify_login("TEST@example.com", "correct-horse-battery")["id"],
            u["id"])

    def test_rejections(self):
        self.db.create_user("a@b.co", "correct-horse-battery")
        for email, pw in [("a@b.co", "correct-horse-battery"),   # duplicate
                          ("A@B.CO", "correct-horse-battery"),   # duplicate, case
                          ("nope", "correct-horse-battery"),     # bad email
                          ("c@d.co", "short")]:                  # weak password
            with self.assertRaises(ValueError, msg=(email, pw)):
                self.db.create_user(email, pw)

    def test_bad_login_paths_return_none(self):
        self.db.create_user("a@b.co", "correct-horse-battery")
        self.assertIsNone(self.db.verify_login("a@b.co", "wrong-password"))
        self.assertIsNone(self.db.verify_login("ghost@b.co", "correct-horse-battery"))

    def test_deactivated_user_cannot_log_in_or_be_fetched(self):
        u = self.db.create_user("a@b.co", "correct-horse-battery")
        with self.db._conn() as c:
            c.execute("UPDATE users SET is_active=0")
        self.assertIsNone(self.db.verify_login("a@b.co", "correct-horse-battery"))
        self.assertIsNone(self.db.get_by_id(u["id"]))

    def test_sql_injection_attempts_are_inert(self):
        self.db.create_user("a@b.co", "correct-horse-battery")
        self.assertIsNone(self.db.verify_login("' OR '1'='1", "x' OR '1'='1"))
        self.assertIsNone(self.db.verify_login("a@b.co' --", "correct-horse-battery"))
        self.assertEqual(self.db.count(), 1)


class CryptoTests(unittest.TestCase):
    def setUp(self):
        from core import crypto
        self.crypto = crypto
        self._old = (os.environ.get("APP_ENCRYPTION_KEY"), crypto._fernet)
        # Use an env-supplied key so tests never create a key file in the
        # real data/ directory.
        os.environ["APP_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        crypto._fernet = None

    def tearDown(self):
        old_env, old_fernet = self._old
        if old_env is None:
            os.environ.pop("APP_ENCRYPTION_KEY", None)
        else:
            os.environ["APP_ENCRYPTION_KEY"] = old_env
        self.crypto._fernet = old_fernet

    def test_roundtrip_and_ciphertext_hides_plaintext(self):
        enc = self.crypto.encrypt("sk-secret-123")
        self.assertTrue(enc.startswith("enc:"))
        self.assertNotIn("secret", enc)
        self.assertEqual(self.crypto.decrypt(enc), "sk-secret-123")

    def test_same_plaintext_encrypts_differently_each_time(self):
        self.assertNotEqual(self.crypto.encrypt("x"), self.crypto.encrypt("x"))

    def test_legacy_plaintext_and_empty_pass_through(self):
        self.assertEqual(self.crypto.decrypt("sk-old-plaintext"), "sk-old-plaintext")
        self.assertEqual(self.crypto.encrypt(""), "")
        self.assertEqual(self.crypto.decrypt(""), "")

    def test_wrong_key_returns_empty_instead_of_raising_or_leaking(self):
        enc = self.crypto.encrypt("sk-secret-123")
        os.environ["APP_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        self.crypto._fernet = None
        self.assertEqual(self.crypto.decrypt(enc), "")

    def test_tampered_ciphertext_is_rejected(self):
        enc = self.crypto.encrypt("sk-secret-123")
        tampered = enc[:-3] + ("AAA" if not enc.endswith("AAA") else "BBB")
        self.assertEqual(self.crypto.decrypt(tampered), "")

    def test_bytes_roundtrip_and_plaintext_passthrough(self):
        blob = b'[{"name":"li_at","value":"abc"}]'
        enc = self.crypto.encrypt_bytes(blob)
        self.assertNotEqual(enc, blob)
        self.assertNotIn(b"li_at", enc)
        self.assertEqual(self.crypto.decrypt_bytes(enc), blob)
        self.assertEqual(self.crypto.decrypt_bytes(blob), blob)   # legacy file


class SettingsEncryptionTests(unittest.TestCase):
    """API keys must be ciphertext in the SQLite file, plaintext through
    the public read API."""

    def setUp(self):
        from core import crypto
        self.crypto = crypto
        self._old = (os.environ.get("APP_ENCRYPTION_KEY"), crypto._fernet)
        os.environ["APP_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        crypto._fernet = None
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        old_env, old_fernet = self._old
        if old_env is None:
            os.environ.pop("APP_ENCRYPTION_KEY", None)
        else:
            os.environ["APP_ENCRYPTION_KEY"] = old_env
        self.crypto._fernet = old_fernet
        self.tmp.cleanup()

    def test_api_key_is_encrypted_on_disk_but_readable_through_api(self):
        from db.settings_db import SettingsDB
        import inspect
        try:
            db = SettingsDB(path=Path(self.tmp.name) / "settings.db")
        except TypeError:
            self.skipTest("SettingsDB constructor doesn't accept a path")

        db.write("openai_api_key", "sk-live-abcdef")
        db.write("openai_model", "gpt-5.6-terra")

        with db._conn() as c:
            raw_key   = c.execute("SELECT value FROM settings WHERE key='openai_api_key'").fetchone()["value"]
            raw_model = c.execute("SELECT value FROM settings WHERE key='openai_model'").fetchone()["value"]

        self.assertTrue(raw_key.startswith("enc:"))
        self.assertNotIn("sk-live", raw_key)
        self.assertEqual(raw_model, "gpt-5.6-terra", "non-secret values stay readable")

        self.assertEqual(db.read("openai_api_key"), "sk-live-abcdef")
        self.assertEqual(db.read_all()["openai_api_key"], "sk-live-abcdef")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SessionSecretTests(unittest.TestCase):
    """The signing key must never be a value that's public in the repo.
    Regression: .env.example shipped 'change_this_to_random_string', which
    the original check (only 'change_me') happily accepted as a real key."""

    @classmethod
    def setUpClass(cls):
        # core.auth imports fastapi at module level; stub it if absent so
        # the secret-selection logic (which doesn't need it) is testable.
        import types
        if "fastapi" not in sys.modules:
            try:
                import fastapi  # noqa: F401
            except Exception:
                stub = types.ModuleType("fastapi")
                stub.Request = type("Request", (), {})
                stub.HTTPException = type("HTTPException", (Exception,), {})
                sys.modules["fastapi"] = stub
        import core.auth as auth
        cls.auth = auth

    def setUp(self):
        from core.config import cfg
        self.cfg = cfg
        self._old = cfg.APP_SECRET_KEY
        self.tmp = tempfile.TemporaryDirectory()
        self._old_path = self.auth._SECRET_PATH
        self.auth._SECRET_PATH = Path(self.tmp.name) / ".session_secret"

    def tearDown(self):
        self.cfg.APP_SECRET_KEY = self._old
        self.auth._SECRET_PATH = self._old_path
        self.tmp.cleanup()

    def test_every_shipped_placeholder_is_rejected_and_replaced(self):
        for placeholder in ("", "change_me", "change_this_to_random_string"):
            self.cfg.APP_SECRET_KEY = placeholder
            self.auth._SECRET_PATH.unlink(missing_ok=True)
            secret = self.auth._load_or_create_secret()
            self.assertNotEqual(secret, placeholder, placeholder)
            self.assertGreaterEqual(len(secret), 32)

    def test_short_custom_secret_is_rejected(self):
        self.cfg.APP_SECRET_KEY = "hunter2"
        self.assertNotEqual(self.auth._load_or_create_secret(), "hunter2")

    def test_strong_custom_secret_is_used_as_is(self):
        strong = "x" * 48
        self.cfg.APP_SECRET_KEY = strong
        self.assertEqual(self.auth._load_or_create_secret(), strong)

    def test_generated_secret_is_stable_across_restarts(self):
        self.cfg.APP_SECRET_KEY = "change_me"
        first  = self.auth._load_or_create_secret()
        second = self.auth._load_or_create_secret()
        self.assertEqual(first, second, "sessions would all die on every restart")

    def test_generated_secret_file_is_owner_only(self):
        if os.name != "posix":
            self.skipTest("POSIX permissions only")
        self.cfg.APP_SECRET_KEY = "change_me"
        self.auth._load_or_create_secret()
        mode = oct(os.stat(self.auth._SECRET_PATH).st_mode)[-3:]
        self.assertEqual(mode, "600")

    def test_tokens_roundtrip_and_reject_tampering(self):
        import core.auth as auth
        auth._serializer = None
        self.cfg.APP_SECRET_KEY = "y" * 48
        token = auth.create_session_token(7)
        self.assertEqual(auth.verify_session_token(token), 7)
        self.assertIsNone(auth.verify_session_token(token[:-2] + "zz"))
        self.assertIsNone(auth.verify_session_token("garbage"))
        auth._serializer = None


class LoginThrottleTests(unittest.TestCase):
    def setUp(self):
        from core.throttle import LoginThrottle
        self.now = [1000.0]
        self.t = LoginThrottle(per_account=5, per_ip=20, window_seconds=900,
                               clock=lambda: self.now[0])

    def _fail(self, ip="1.1.1.1", email="a@b.co", n=1):
        for _ in range(n):
            self.t.record_failure(ip, email)

    def test_allows_up_to_limit_then_blocks_that_account(self):
        self._fail(n=4)
        self.assertEqual(self.t.retry_after("1.1.1.1", "a@b.co"), 0)
        self._fail(n=1)
        self.assertGreater(self.t.retry_after("1.1.1.1", "a@b.co"), 0)

    def test_block_is_scoped_to_that_ip_and_email(self):
        self._fail(n=5)
        self.assertEqual(self.t.retry_after("2.2.2.2", "a@b.co"), 0)   # other IP
        self.assertEqual(self.t.retry_after("1.1.1.1", "x@y.co"), 0)   # other email

    def test_email_case_and_whitespace_dont_bypass_the_limit(self):
        self._fail(email="a@b.co", n=5)
        self.assertGreater(self.t.retry_after("1.1.1.1", "  A@B.CO "), 0)

    def test_password_spraying_across_many_emails_trips_ip_limit(self):
        for i in range(20):
            self._fail(email=f"user{i}@x.co")            # 1 failure each
        # Each individual account is still under ITS limit (1 failure)...
        self.assertLess(len(self.t._fails["acct:1.1.1.1|user0@x.co"]), 5)
        self.assertGreater(self.t.retry_after("1.1.1.1", "brand-new@x.co"), 0,
                           "spraying many accounts must lock the source IP")

    def test_window_expires(self):
        self._fail(n=5)
        self.assertGreater(self.t.retry_after("1.1.1.1", "a@b.co"), 0)
        self.now[0] += 901
        self.assertEqual(self.t.retry_after("1.1.1.1", "a@b.co"), 0)

    def test_retry_after_counts_down(self):
        self._fail(n=5)
        first = self.t.retry_after("1.1.1.1", "a@b.co")
        self.now[0] += 100
        self.assertLess(self.t.retry_after("1.1.1.1", "a@b.co"), first)

    def test_success_clears_account_counter_but_not_ip_counter(self):
        self._fail(n=4)
        self.t.record_success("1.1.1.1", "a@b.co")
        self._fail(n=4)                                   # would be 8 total without the reset
        self.assertEqual(self.t.retry_after("1.1.1.1", "a@b.co"), 0)
        # ...but the per-IP budget was NOT refunded by that success:
        self._fail(email="other@x.co", n=12)              # 8 + 12 = 20
        self.assertGreater(self.t.retry_after("1.1.1.1", "fresh@x.co"), 0)

    def test_memory_is_bounded_under_key_flooding(self):
        import core.throttle as th
        old = th._MAX_TRACKED_KEYS
        th._MAX_TRACKED_KEYS = 100
        try:
            for i in range(1000):
                self.t.record_failure(f"9.9.{i // 250}.{i % 250}", f"e{i}@x.co")
            self.assertLessEqual(len(self.t._fails), 100 + 2)
        finally:
            th._MAX_TRACKED_KEYS = old
