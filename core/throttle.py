"""
core/throttle.py — login brute-force protection.

Two independent limits, because they stop different attacks:

  per (IP, email)  — 5 failures / 15 min. Stops guessing one account's
                     password from one place.
  per IP           — 20 failures / 15 min. Stops "password spraying":
                     trying one common password across many emails,
                     which never trips the per-account limit.

A successful login clears that (IP, email) counter but deliberately NOT
the per-IP one, so an attacker can't reset their spray budget by
logging into an account of their own between attempts.

In-memory and per-process: it resets on restart and isn't shared across
multiple worker processes. That's an acceptable trade for a single
uvicorn process on one VM; move it to Redis if this ever runs as
several workers.

Behind a reverse proxy the "IP" the app sees is the proxy's unless
uvicorn is started with --proxy-headers (and --forwarded-allow-ips set
to the proxy) — otherwise every user shares one bucket and one bad
actor locks everyone out. See deployment notes.
"""

import time
from typing import Callable

_MAX_TRACKED_KEYS = 10_000     # hard memory cap for the whole table


class LoginThrottle:
    def __init__(self, per_account: int = 5, per_ip: int = 20,
                 window_seconds: int = 900,
                 clock: Callable[[], float] = time.monotonic):
        self.per_account = per_account
        self.per_ip      = per_ip
        self.window      = window_seconds
        self._clock      = clock
        self._fails: dict[str, list[float]] = {}

    @staticmethod
    def _keys(ip: str, email: str) -> tuple[str, str]:
        return f"acct:{ip}|{email.strip().lower()}", f"ip:{ip}"

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._fails.get(key, []) if now - t < self.window]
        if recent:
            self._fails[key] = recent
        else:
            self._fails.pop(key, None)
        return recent

    def retry_after(self, ip: str, email: str) -> int:
        """Seconds until this attempt would be allowed (0 = allowed now)."""
        now = self._clock()
        wait = 0
        for key, limit in zip(self._keys(ip, email), (self.per_account, self.per_ip)):
            recent = self._recent(key, now)
            if len(recent) >= limit:
                wait = max(wait, int(self.window - (now - recent[0])) + 1)
        return wait

    def record_failure(self, ip: str, email: str) -> None:
        now = self._clock()
        if len(self._fails) >= _MAX_TRACKED_KEYS:
            # Someone is flooding distinct keys to exhaust memory. Drop
            # everything expired first; if still full, drop the oldest.
            for k in list(self._fails):
                self._recent(k, now)
            while len(self._fails) >= _MAX_TRACKED_KEYS:
                self._fails.pop(next(iter(self._fails)))
        for key in self._keys(ip, email):
            self._fails.setdefault(key, []).append(now)

    def record_success(self, ip: str, email: str) -> None:
        self._fails.pop(self._keys(ip, email)[0], None)


# One shared instance for the process.
login_throttle = LoginThrottle()
