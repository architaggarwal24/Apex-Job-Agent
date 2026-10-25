# tests/test_notifications.py
"""
tests/test_notifications.py — "notify me when the agent needs me" (core/notifier.py,
db/notifications_db.py, server/routes/notifications.py, the secret-key rule in
db/settings_db.py and the SMTP host check in core/urlcheck.py).

A FAKE CLOCK moved by hand, FAKE TRANSPORTS that record what would have been sent (or fail on
purpose), real SQLite in a temp directory. Nothing touches a mail server, Telegram or a model.
Covered:

  * batching: ready_for_review is a digest; several lines of one kind go in one message;
  * the limit of one message per kind per 10 minutes;
  * quiet hours (also across midnight): nothing is sent, and the backlog goes out after;
  * per-event toggles, owner-only, "no channel set up = nothing queued";
  * the sources: stage changes (needs_you, ready_for_review, autonomy submitted), due
    follow-ups and an interview within 24 h, each told once;
  * delivery failures are logged by type and never raised;
  * secrets: *_token / *_password are encrypted at rest, masked, never returned;
  * the SMTP host gets the metadata-address protection; the routes are owner-only.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from applier import autonomy
from core import notifier
from core.notifier import Notifier
from core.urlcheck import validate_smtp_host
from db import jobs_db as jobs_module
from db.jobs_db import JobsDB
from db.notifications_db import NotificationStore
from db.settings_db import SettingsDB, is_secret_key

START = datetime(2026, 10, 5, 12, 0, 0)            # a Monday, noon
TOKEN = "123456789:AAFakeTelegramBotTokenForTests_1234"
PASSWORD = "smtp-pass-SECRET-987"


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class FakeSettings:
    def __init__(self, **values):
        self.values = {"notify_telegram_enabled": "true", "notify_telegram_bot_token": TOKEN,
                       "notify_telegram_chat_id": "424242", **values}

    def read(self, key, default=""):
        return self.values.get(key, default)


class Transport:
    def __init__(self, fail: Exception | None = None):
        self.sent, self.fail = [], fail

    def __call__(self, conf, subject, text):
        if self.fail is not None:
            raise self.fail
        self.sent.append((subject, text))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "jobs.db"
        self.clock = Clock()
        self.settings = FakeSettings()
        self.tg, self.mail = Transport(), Transport()
        self.n = 0
        self.notifier = Notifier(self.path, settings=self.settings, clock=self.clock,
                                 transports={"telegram": self.tg, "email": self.mail})
        listener = lambda path, uid, event: self.notifier.on_stage_change(uid, event)
        jobs_module.add_stage_listener(listener)
        self.addCleanup(jobs_module.remove_stage_listener, listener)

    def jobs(self, uid=1):
        return JobsDB(path=self.path, user_id=uid, clock=self.clock)

    def job(self, uid=1, title=None):
        self.n += 1
        return self.jobs(uid).add({"url": f"https://jobs.example/{uid}/{self.n}", "title": title or f"Engineer {self.n}",
                                   "company": "Acme", "source": "linkedin"})

    def to_review(self, uid=1):
        j = self.job(uid)
        db = self.jobs(uid)
        db.transition(j, "preparing", actor="agent")
        db.transition(j, "ready_for_review", actor="agent")
        return j

    def to_needs_you(self, uid=1):
        j = self.job(uid)
        db = self.jobs(uid)
        db.transition(j, "preparing", actor="agent")
        db.transition(j, "needs_you", actor="agent", reason="captcha")
        return j

    def flush(self):
        return self.notifier.flush()

    def outbox(self):
        NotificationStore(1, self.path)                         # the table exists even if nothing was queued
        with closing(sqlite3.connect(str(self.path))) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM notification_outbox ORDER BY id").fetchall()]


# ── batching and the 10-minute limit ──────────────────────────────

class BatchingTests(Base):
    def test_ready_for_review_is_a_digest(self):
        for _ in range(3):
            self.to_review()
            self.clock.advance(minutes=2)
        self.assertEqual(self.flush(), [])                      # oldest has waited 6 minutes
        self.assertEqual(self.tg.sent, [])
        self.clock.advance(minutes=4)                           # oldest: 10 minutes
        self.assertEqual(self.flush(), ["ready_for_review"])
        self.assertEqual(len(self.tg.sent), 1)
        subject, text = self.tg.sent[0]
        self.assertEqual(subject, "Apex: 3 applications ready for your review")
        self.assertEqual(text.count("is ready for review"), 3)
        self.assertEqual({r["status"] for r in self.outbox()}, {"sent"})

    def test_needs_you_goes_at_once_then_at_most_once_per_ten_minutes(self):
        self.to_needs_you()
        self.assertEqual(self.flush(), ["needs_you"])
        self.assertIn("needs you (captcha)", self.tg.sent[0][1])
        self.to_needs_you()
        self.clock.advance(minutes=3)
        self.to_needs_you()
        self.assertEqual(self.flush(), [])                      # 3 minutes since the last one
        self.clock.advance(minutes=6, seconds=59)
        self.assertEqual(self.flush(), [])                      # still under 10
        self.clock.advance(seconds=1)
        self.assertEqual(self.flush(), ["needs_you"])
        self.assertEqual(len(self.tg.sent), 2)
        self.assertEqual(self.tg.sent[1][0], "Apex: 2 applications need you")

    def test_the_limit_is_per_kind(self):
        self.to_needs_you()
        self.flush()
        j = self.job()
        db = self.jobs()
        db.transition(j, "submitted", actor="user")
        db.transition(j, "technical_round", actor="user")
        db.set_hunt(j, {"next_step_at": (START + timedelta(hours=3)).isoformat(timespec="minutes")})
        self.notifier.tick()
        self.assertEqual([s for s, _ in self.tg.sent], ["Apex: 1 application needs you",
                                                         "Apex: 1 interview in the next 24 hours"])

    def test_every_enabled_channel_gets_the_message(self):
        self.settings.values.update(notify_email_enabled="true", notify_smtp_host="smtp.example.com",
                                    notify_email_to="me@example.com", notify_email_from="agent@example.com")
        self.to_needs_you()
        self.flush()
        self.assertEqual((len(self.tg.sent), len(self.mail.sent)), (1, 1))


# ── quiet hours ───────────────────────────────────────────────────

class QuietHoursTests(Base):
    def test_in_quiet_hours(self):
        at = lambda h, m=0: datetime(2026, 10, 5, h, m)
        self.assertTrue(notifier.in_quiet_hours(at(23), "22:00", "07:00"))
        self.assertTrue(notifier.in_quiet_hours(at(6, 59), "22:00", "07:00"))
        self.assertFalse(notifier.in_quiet_hours(at(7), "22:00", "07:00"))
        self.assertFalse(notifier.in_quiet_hours(at(21, 59), "22:00", "07:00"))
        self.assertTrue(notifier.in_quiet_hours(at(13), "12:30", "14:00"))
        self.assertFalse(notifier.in_quiet_hours(at(14), "12:30", "14:00"))
        self.assertFalse(notifier.in_quiet_hours(at(13), "", ""))
        self.assertFalse(notifier.in_quiet_hours(at(13), "09:00", "09:00"))

    def test_nothing_is_sent_in_quiet_hours_and_the_backlog_goes_out_after(self):
        self.settings.values.update(notify_quiet_start="22:00", notify_quiet_end="07:00")
        self.clock.now = datetime(2026, 10, 5, 23, 0)
        self.to_needs_you()
        self.to_review()
        self.clock.advance(hours=1)
        self.to_needs_you()
        self.clock.now = datetime(2026, 10, 6, 6, 59)
        self.assertEqual(self.flush(), [])
        self.assertEqual(self.tg.sent, [])
        self.clock.now = datetime(2026, 10, 6, 7, 0)
        self.assertEqual(sorted(self.flush()), ["needs_you", "ready_for_review"])
        self.assertEqual(sorted(s for s, _ in self.tg.sent),
                         ["Apex: 1 application ready for your review", "Apex: 2 applications need you"])


# ── toggles, owner-only, no channel ───────────────────────────────

class ToggleTests(Base):
    def test_a_switched_off_event_is_not_queued(self):
        self.settings.values["notify_event_needs_you"] = "false"
        self.to_needs_you()
        self.to_review()
        self.assertEqual([r["kind"] for r in self.outbox()], ["ready_for_review"])

    def test_switching_off_after_queueing_drops_it(self):
        self.to_needs_you()
        self.settings.values["notify_event_needs_you"] = "false"
        self.assertEqual(self.flush(), [])
        self.assertEqual(self.outbox()[0]["status"], "dropped")
        self.assertEqual(self.tg.sent, [])

    def test_each_toggle_is_its_own(self):
        for kind in notifier.KINDS:
            self.assertEqual(notifier.DEFAULTS[f"notify_event_{kind}"], "true")
        self.settings.values["notify_event_ready_for_review"] = "false"
        self.to_needs_you()
        self.assertEqual([r["kind"] for r in self.outbox()], ["needs_you"])

    def test_only_the_owners_own_cards(self):
        self.to_needs_you(uid=2)
        self.to_review(uid=2)
        self.assertEqual(self.outbox(), [])

    def test_nothing_is_queued_without_a_channel(self):
        self.settings.values["notify_telegram_enabled"] = "false"
        self.to_needs_you()
        self.assertEqual(self.outbox(), [])
        self.assertEqual(notifier.channels(notifier.read_config(self.settings)), [])


# ── the sources ───────────────────────────────────────────────────

class SourceTests(Base):
    def test_a_due_follow_up_is_told_once(self):
        j = self.job()
        self.jobs().transition(j, "submitted", actor="user")
        self.notifier.tick()
        self.assertEqual(self.tg.sent, [])                      # check-in is due in 7 days
        self.clock.advance(days=7)
        self.notifier.tick()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("Follow-up due (check in)", self.tg.sent[0][1])
        self.clock.advance(minutes=30)
        self.notifier.tick()
        self.assertEqual(len(self.tg.sent), 1)                  # still due, but already told

    def test_an_interview_within_24_hours(self):
        soon, later, other = self.job(), self.job(), self.job()
        db = self.jobs()
        for j in (soon, later, other):
            db.transition(j, "submitted", actor="user")
        db.transition(soon, "technical_round", actor="user")
        db.transition(later, "hiring_manager", actor="user")
        db.set_hunt(soon, {"next_step_at": (START + timedelta(hours=20)).isoformat(timespec="minutes"),
                           "next_step_text": "Tech round with Dana"})
        db.set_hunt(later, {"next_step_at": (START + timedelta(hours=30)).isoformat(timespec="minutes")})
        db.set_hunt(other, {"next_step_at": (START + timedelta(hours=2)).isoformat(timespec="minutes")})  # not an interview stage
        self.notifier.tick()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("Tech round with Dana", self.tg.sent[0][1])
        self.assertEqual(sum(line.startswith("- ") for line in self.tg.sent[0][1].splitlines()), 1)
        self.clock.advance(hours=7)                             # `later` is now 23 h away
        self.notifier.tick()
        interviews = [t for s, t in self.tg.sent if "interview" in s]   # (the passed next step also earns a follow-up)
        self.assertEqual(len(interviews), 2)
        self.assertNotIn("Tech round with Dana", interviews[1])

    def test_a_date_only_interview_counts_for_that_day(self):
        j = self.job()
        db = self.jobs()
        db.transition(j, "submitted", actor="user")
        db.transition(j, "recruiter_screen", actor="user")
        db.set_hunt(j, {"next_step_at": (START + timedelta(days=1)).date().isoformat()})
        self.notifier.tick()
        self.assertEqual(len(self.tg.sent), 1)

    def test_autonomy_submitted(self):
        j = self.to_review()
        db = self.jobs()
        db.record_step(j, "autonomy_approved", "Auto-approved by autonomy rule #1",
                       {"rule_id": 1, "dry_run": False}, actor="autonomy")
        db.transition(j, "submitted", actor="agent")
        kinds = [r["kind"] for r in self.outbox()]
        self.assertEqual(kinds, ["ready_for_review", "autonomy_submitted"])

    def test_a_person_submitting_is_not_autonomy(self):
        j = self.to_review()
        self.jobs().transition(j, "submitted", actor="user")
        self.assertEqual([r["kind"] for r in self.outbox()], ["ready_for_review"])

    def test_auto_submitted_reads_the_timeline(self):
        e = lambda i, kind, to, step=None, dry=False: {"id": i, "kind": kind, "to_stage": to,
                                                       "detail": {"step": step, "dry_run": dry} if step else {}}
        approved = [e(1, "transition", "ready_for_review"), e(2, "step", "ready_for_review", "autonomy_approved"),
                    e(3, "transition", "submitted")]
        self.assertTrue(autonomy.auto_submitted(approved, 3))
        dry = [e(1, "transition", "ready_for_review"), e(2, "step", "ready_for_review", "autonomy_approved", True),
               e(3, "transition", "submitted")]
        self.assertFalse(autonomy.auto_submitted(dry, 3))
        stale = [e(1, "transition", "ready_for_review"), e(2, "step", "ready_for_review", "autonomy_approved"),
                 e(3, "transition", "needs_you"), e(4, "transition", "submitted")]
        self.assertFalse(autonomy.auto_submitted(stale, 4))
        self.assertFalse(autonomy.auto_submitted([e(1, "transition", "submitted")], 1))

    def test_the_same_news_twice_is_one_line(self):
        store = NotificationStore(1, self.path)
        self.assertTrue(store.enqueue("follow_up_due", "k", "x", START))
        self.assertFalse(store.enqueue("follow_up_due", "k", "x", START))
        with self.assertRaises(ValueError):
            store.enqueue("bogus", "k", "x", START)
        self.assertEqual(NotificationStore(2, self.path).pending(), [])          # per user


# ── failures ──────────────────────────────────────────────────────

class FailureTests(Base):
    def test_a_failing_channel_is_logged_by_type_and_never_raised(self):
        self.tg.fail = RuntimeError(f"https://api.telegram.org/bot{TOKEN}/sendMessage refused")
        self.to_needs_you()
        with self.assertLogs("core.notifier", "WARNING") as logs:
            self.assertEqual(self.flush(), ["needs_you"])
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(self.outbox()[0]["status"], "failed")

    def test_one_channel_failing_does_not_stop_the_other(self):
        self.settings.values.update(notify_email_enabled="true", notify_smtp_host="smtp.example.com",
                                    notify_email_to="me@example.com", notify_smtp_username="me@example.com")
        self.mail.fail = OSError("connection refused")
        self.to_needs_you()
        self.flush()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertEqual(self.outbox()[0]["status"], "sent")

    def test_a_broken_notifier_never_fails_the_stage_change(self):
        broken = Notifier(self.path, settings=self.settings, clock=self.clock, transports={"telegram": self.tg})
        broken.store = lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error"))
        listener = lambda path, uid, event: broken.on_stage_change(uid, event)
        jobs_module.add_stage_listener(listener)
        self.addCleanup(jobs_module.remove_stage_listener, listener)
        with self.assertLogs("core.notifier", "WARNING") as logs:
            j = self.to_needs_you()
            self.assertEqual(broken.flush(), [])
        self.assertEqual(self.jobs().get_stage(j), "needs_you")
        self.assertIn("could not queue a needs_you notification: OperationalError", "\n".join(logs.output))

    def test_real_transports_never_leak_the_secret_into_logs(self):
        conf = notifier.read_config(FakeSettings())
        with mock.patch("requests.post", side_effect=ConnectionError(f"bot{TOKEN} unreachable")):
            with self.assertLogs("core.notifier", "WARNING") as logs:
                ok = notifier.deliver(conf, ["telegram"], "s", "t")
        self.assertEqual(ok, [])
        self.assertNotIn(TOKEN, "\n".join(logs.output))


# ── secrets, validation, the SMTP host ────────────────────────────

class SecretsAndValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = SettingsDB(path=Path(self.tmp.name) / "settings.db")

    def raw(self, key):
        with closing(sqlite3.connect(str(self.db.path))) as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def test_the_secret_rule(self):
        for key in ("nvidia_api_key", "custom_x_api_key", "notify_telegram_bot_token", "notify_smtp_password",
                    "anything_token", "other_password"):
            self.assertTrue(is_secret_key(key), key)
        for key in ("notify_smtp_host", "notify_email_to", "token_count", "password_hint", "llm_provider"):
            self.assertFalse(is_secret_key(key), key)

    def test_token_and_password_are_encrypted_at_rest(self):
        self.db.write("notify_telegram_bot_token", TOKEN)
        self.db.write("notify_smtp_password", PASSWORD)
        self.db.write("notify_smtp_host", "smtp.example.com")
        for key, plain in (("notify_telegram_bot_token", TOKEN), ("notify_smtp_password", PASSWORD)):
            stored = self.raw(key)
            self.assertTrue(stored.startswith("enc:"), key)
            self.assertNotIn(plain, stored)
            self.assertEqual(self.db.read(key), plain)
        self.assertEqual(self.raw("notify_smtp_host"), "smtp.example.com")
        self.assertEqual(self.db.read_all()["notify_smtp_password"], PASSWORD)

    def test_public_config_never_returns_a_secret(self):
        self.db.write("notify_telegram_bot_token", TOKEN)
        self.db.write("notify_smtp_password", PASSWORD)
        pub = notifier.public_config(self.db)
        self.assertEqual((pub["notify_telegram_bot_token"], pub["notify_smtp_password"]), (notifier.MASK, notifier.MASK))
        self.assertNotIn(TOKEN, str(pub))
        self.assertNotIn(PASSWORD, str(pub))
        self.assertEqual(notifier.public_config(SettingsDB(path=Path(self.tmp.name) / "s2.db"))["notify_smtp_password"], "")

    def test_smtp_host_has_the_metadata_protection(self):
        for bad in ("169.254.169.254", "2852039166", "0xa9fea9fe", "0251.0376.0251.0376", "metadata.google.internal",
                    "[fe80::1]", "::ffff:169.254.169.254", "0.0.0.0", "smtp://smtp.example.com",
                    "smtp.example.com:587", "user@smtp.example.com", "smtp.example.com/x", "smtp example.com", ""):
            with self.assertRaises(ValueError, msg=bad):
                validate_smtp_host(bad)
        for good, want in (("smtp.gmail.com", "smtp.gmail.com"), ("SMTP.Example.COM.", "smtp.example.com"),
                           ("localhost", "localhost"), ("192.168.1.10", "192.168.1.10"), ("[::1]", "::1")):
            self.assertEqual(validate_smtp_host(good), want)

    def test_send_email_checks_the_host_again(self):
        conf = dict(notifier.DEFAULTS, notify_smtp_host="169.254.169.254", notify_email_to="me@example.com",
                    notify_email_from="a@example.com")
        with mock.patch("smtplib.SMTP") as smtp:
            with self.assertRaises(ValueError):
                notifier.send_email(conf, "s", "t")
        smtp.assert_not_called()

    def test_clean_updates(self):
        good = notifier.clean_updates({"smtp_host": "smtp.example.com", "smtp_port": 465, "smtp_security": "SSL",
                                       "email_to": "me@example.com", "notify_email_enabled": True,
                                       "event_needs_you": "false", "telegram_chat_id": "-100123",
                                       "quiet_start": "22:00", "quiet_end": "07:00"})
        self.assertEqual(good["notify_smtp_port"], "465")
        self.assertEqual(good["notify_smtp_security"], "ssl")
        self.assertEqual(good["notify_email_enabled"], "true")
        self.assertEqual(good["notify_event_needs_you"], "false")
        for bad in ({"smtp_host": "169.254.169.254"}, {"smtp_port": 0}, {"smtp_port": True}, {"smtp_security": "tls1"},
                    {"email_to": "not an address"}, {"email_to": "a@b.c\nBcc: x@y.z"}, {"telegram_chat_id": "abc"},
                    {"telegram_bot_token": "nope"}, {"quiet_start": "25:00", "quiet_end": "07:00"},
                    {"quiet_start": "22:00"}, {"event_needs_you": "maybe"}, {"bogus": 1}, {}):
            with self.assertRaises(ValueError, msg=bad):
                notifier.clean_updates(bad)
        self.assertEqual(notifier.clean_updates({"smtp_password": notifier.MASK, "smtp_port": 25}),
                         {"notify_smtp_port": "25"})                           # masked secret: left alone
        self.assertEqual(notifier.clean_updates({"smtp_password": None}), {"notify_smtp_password": ""})


# ── HTTP ──────────────────────────────────────────────────────────

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class NotificationsHttpTests(unittest.TestCase):
    def setUp(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self._saved = (cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE,
                       cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer, SettingsDB._instance)
        SettingsDB._instance = SettingsDB(path=tmp / "settings.db")
        cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR = tmp / "jobs.db", tmp / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP = "test-signing-secret-" + "x" * 40, False, False
        UsersDB._instance, auth._serializer = UsersDB(path=tmp / "users.db"), None
        self.tg = Transport()
        p = mock.patch.dict(notifier.TRANSPORTS, {"telegram": self.tg, "email": Transport(OSError(PASSWORD))})
        p.start()
        self.addCleanup(p.stop)
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
        (cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP,
         UsersDB._instance, auth._serializer, SettingsDB._instance) = self._saved
        self.tmp.cleanup()

    def test_settings_routes_mask_secrets_and_validate(self):
        r = self.alice.put("/api/notifications/settings",
                           json={"telegram_enabled": True, "telegram_bot_token": TOKEN, "telegram_chat_id": "424242",
                                 "smtp_password": PASSWORD, "smtp_host": "smtp.example.com"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(TOKEN, r.text)
        self.assertNotIn(PASSWORD, r.text)
        self.assertEqual(r.json()["channels_ready"], ["telegram"])
        g = self.alice.get("/api/notifications/settings")
        self.assertEqual(g.json()["notify_telegram_bot_token"], notifier.MASK)
        self.assertNotIn(TOKEN, g.text)
        self.assertEqual(SettingsDB.get().read("notify_telegram_bot_token"), TOKEN)

        bad = self.alice.put("/api/notifications/settings", json={"smtp_host": "169.254.169.254", "smtp_port": 25})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(SettingsDB.get().read("notify_smtp_port", "587"), "587")    # nothing half-saved

        # the generic settings endpoints neither save nor reveal them
        self.assertEqual(self.alice.post("/api/settings", json={"notify_smtp_host": "169.254.169.254"}).status_code, 400)
        self.assertEqual(self.alice.get("/api/settings/raw/notify_telegram_bot_token").status_code, 403)
        self.assertEqual(self.alice.get("/api/settings/raw/notify_smtp_password").status_code, 403)
        all_settings = self.alice.get("/api/settings").text
        self.assertNotIn(TOKEN, all_settings)
        self.assertNotIn(PASSWORD, all_settings)

    def test_test_buttons(self):
        self.alice.put("/api/notifications/settings",
                       json={"telegram_bot_token": TOKEN, "telegram_chat_id": "424242"})
        r = self.alice.post("/api/notifications/test/telegram")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "ok"))
        self.assertEqual(len(self.tg.sent), 1)
        r = self.alice.post("/api/notifications/test/email",
                            json={"smtp_host": "smtp.example.com", "email_to": "me@example.com",
                                  "email_from": "a@example.com"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("OSError", r.text)
        self.assertNotIn(PASSWORD, r.text)
        self.assertEqual(self.alice.post("/api/notifications/test/email", json={"smtp_host": "169.254.169.254"}).status_code, 400)
        self.assertEqual(self.alice.post("/api/notifications/test/pigeon").status_code, 404)

    def test_owner_only(self):
        self.assertEqual(self.bob.get("/api/notifications/settings").status_code, 403)
        self.assertEqual(self.bob.put("/api/notifications/settings", json={"smtp_port": 25}).status_code, 403)
        self.assertEqual(self.bob.post("/api/notifications/test/telegram").status_code, 403)
        self.assertEqual(TestClient(self.app).get("/api/notifications/settings").status_code, 401)


if __name__ == "__main__":
    unittest.main()
