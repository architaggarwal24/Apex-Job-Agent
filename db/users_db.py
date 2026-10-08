"""
db/users_db.py — user accounts for multi-user deployment.

Password hashing: scrypt, via Python's standard-library hashlib — no
new dependency (argon2-cffi would need a C extension that isn't always
trivial to build on every host; bcrypt has a 72-byte password limit
people don't expect). scrypt is an OWASP-acceptable, memory-hard KDF;
this is the same class of algorithm as argon2/bcrypt, just the version
available without adding a package. Format stores the algorithm name
and cost parameters alongside the hash (scrypt:n:r:p:salt:hash) so the
cost can be raised later for new hashes without invalidating old ones.

Never logs or stores a plaintext password anywhere, even transiently.
"""

import hashlib
import hmac
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.config import cfg

# Cost parameters for interactive (web login) use — a few tens of ms per
# hash on typical server hardware. Raise N (must stay a power of 2) for
# more brute-force resistance if your host has CPU/memory to spare; this
# only affects newly-created hashes, existing ones keep working.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _DKLEN = 2 ** 14, 8, 1, 32

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N,
                        r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return f"scrypt:{_SCRYPT_N}:{_SCRYPT_R}:{_SCRYPT_P}:{salt.hex()}:{dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split(":")
        if algo != "scrypt":
            return False
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=int(n),
                            r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


class UsersDB:
    _instance = None

    def __init__(self, path: Path | None = None):
        self.path = path or (cfg.DATA_DIR / "users.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @classmethod
    def get(cls) -> "UsersDB":
        """Singleton — reuse one instance per process."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    email         TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    is_active     INTEGER NOT NULL DEFAULT 1
                )
            """)
            # Bumped on password change / "sign out everywhere". Every session
            # cookie carries the version it was issued at, and is rejected once
            # the user's version has moved on — the only way to revoke an
            # otherwise-stateless signed cookie.
            try:
                c.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass   # already there

    def create_user(self, email: str, password: str) -> dict:
        email = email.strip().lower()
        if not _EMAIL_RE.match(email):
            raise ValueError("Enter a valid email address")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")

        pw_hash = _hash_password(password)
        now = datetime.now().isoformat()
        try:
            with self._conn() as c:
                cur = c.execute(
                    "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                    (email, pw_hash, now),
                )
                return {"id": cur.lastrowid, "email": email}
        except sqlite3.IntegrityError:
            raise ValueError("An account with that email already exists")

    def verify_login(self, email: str, password: str) -> dict | None:
        """
        Returns the user dict on success, None on bad email OR bad
        password — deliberately not distinguishing the two to the
        caller. Which one failed is exactly the information that lets
        an attacker enumerate which emails have accounts.
        """
        email = email.strip().lower()
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM users WHERE email=? AND is_active=1", (email,)
            ).fetchone()
        if not row:
            _hash_password("dummy-password-to-equalize-timing")
            return None
        if not _verify_password(password, row["password_hash"]):
            return None
        return {"id": row["id"], "email": row["email"], "session_version": row["session_version"]}

    def get_by_id(self, user_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT id, email, created_at, session_version FROM users WHERE id=? AND is_active=1",
                (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def change_password(self, user_id: int, current_password: str, new_password: str) -> int:
        """Set a new password and revoke every existing session. Returns the new
        session version, which the caller must put in the user's fresh cookie.

        PermissionError → the current password is wrong; ValueError → the new
        one isn't acceptable. Kept distinct so the route can answer 403 vs 400
        (and NOT 401, which the web app treats as "your session expired").
        """
        if len(new_password) < 8:
            raise ValueError("New password must be at least 8 characters")
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
            if row is None:
                raise LookupError("Account not found")
            if not _verify_password(current_password, row["password_hash"]):
                raise PermissionError("Current password is incorrect")
            if current_password == new_password:
                raise ValueError("New password must be different from the current one")
            c.execute("UPDATE users SET password_hash=?, session_version=session_version+1 WHERE id=?",
                      (_hash_password(new_password), user_id))
            return row["session_version"] + 1

    def bump_session_version(self, user_id: int) -> int:
        """Invalidate every session for this user ("sign out everywhere")."""
        with self._conn() as c:
            c.execute("UPDATE users SET session_version=session_version+1 WHERE id=? AND is_active=1", (user_id,))
            row = c.execute("SELECT session_version FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise LookupError("Account not found")
        return row["session_version"]

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
