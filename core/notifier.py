# core/notifier.py
"""
core/notifier.py — tell you when the agent needs you, by e-mail (SMTP) and/or a Telegram bot.

WHAT IS TOLD (each can be switched off: notify_event_<kind>, default on):

    needs_you            a card moved to needs_you                     (a stage change)
    ready_for_review     a card is ready for review - always a DIGEST: lines are collected and
                         go out together once the oldest has waited DIGEST_WINDOW (10 minutes)
    follow_up_due        a follow-up (db/follow_ups_db.py) is due        (the periodic check)
    interview_soon       a card at an interview stage has its next_step_at within 24 hours
                                                                         (the periodic check)
    autonomy_submitted   an autonomy rule approved it and it was submitted (applier/autonomy.py)

Only the OWNER's own cards are told about (the settings are the owner's, and so is the inbox).

HOW. Each piece of news is one line in the outbox (db/notifications_db.py), unique per kind +
item, so the periodic check seeing the same due follow-up every minute tells you once. flush()
then sends AT MOST ONE MESSAGE PER KIND PER 10 MINUTES (RATE_LIMIT): everything pending of a
kind goes in that one message. Nothing is sent during the QUIET HOURS (notify_quiet_start /
notify_quiet_end, local "HH:MM", may wrap midnight): the lines wait and go out, batched, when
they end. A line is only queued while its event is on and at least one channel is set up.

DELIVERY FAILURES are logged by exception TYPE only and never raised: a mail server or bot that
is down never disturbs the stage change or the loop that noticed the news. (An exception's text
can carry the bot token - it is part of Telegram's URL - or the SMTP reply; neither is logged.)

SECRETS: notify_smtp_password and notify_telegram_bot_token end in _password / _token, so
db/settings_db.is_secret_key encrypts them at rest; public_config() never returns them, only
whether they are set. The SMTP host gets the same metadata-address protection as provider URLs
(core/urlcheck.validate_smtp_host), checked when saved AND again before connecting.

Testable without waiting or a network: the clock, the settings and the transports
(channel -> fn(config, subject, text)) are all injectable, and tick() is one synchronous round
(periodic checks, then flush) - the background thread is just tick() in a loop.
"""

import logging
import re
import smtplib
import ssl
import threading
from datetime import datetime, time, timedelta
from email.message import EmailMessage

from core.urlcheck import validate_smtp_host
from db import hunt
from db.answer_bank_db import PROFILE_OWNER_ID as OWNER_ID
from db.notifications_db import KINDS, NotificationStore

logger = logging.getLogger(__name__)

RATE_LIMIT = timedelta(minutes=10)
DIGEST_WINDOW = timedelta(minutes=10)
INTERVIEW_WINDOW = timedelta(hours=24)
INTERVIEW_STAGES = ("recruiter_screen", "technical_round", "hiring_manager")
TICK_SECONDS = 60
SEND_TIMEOUT = 15
MAX_LINES = 20
MASK = "••••••••"

PREFIX = "notify_"
DEFAULTS = {
    "notify_email_enabled": "false",
    "notify_smtp_host": "",
    "notify_smtp_port": "587",
    "notify_smtp_security": "starttls",
    "notify_smtp_username": "",
    "notify_smtp_password": "",
    "notify_email_from": "",
    "notify_email_to": "",
    "notify_telegram_enabled": "false",
    "notify_telegram_bot_token": "",
    "notify_telegram_chat_id": "",
    "notify_quiet_start": "",
    "notify_quiet_end": "",
    **{f"notify_event_{k}": "true" for k in KINDS},
}
SECRETS = ("notify_smtp_password", "notify_telegram_bot_token")
SECURITY = ("starttls", "ssl", "none")

_EMAIL = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+$")
_TOKEN = re.compile(r"^\d{3,20}:[A-Za-z0-9_-]{20,100}$")
_CHAT = re.compile(r"^(-?\d{1,20}|@[A-Za-z0-9_]{5,32})$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

SUBJECTS = {
    "needs_you": "{n} application{s} need{v} you",
    "ready_for_review": "{n} application{s} ready for your review",
    "follow_up_due": "{n} follow-up{s} due",
    "interview_soon": "{n} interview{s} in the next 24 hours",
    "autonomy_submitted": "{n} application{s} submitted by an autonomy rule",
}


class NotifyError(Exception):
    """A test message could not be sent. The message is safe to show (no secrets)."""


class DeliveryError(Exception):
    """A transport answered with a failure. Carries no secret (never the URL or the reply)."""


# ── settings ──────────────────────────────────────────────────────

def _read(settings, key: str) -> str:
    try:
        value = settings.read(key, DEFAULTS[key])
    except Exception:
        value = DEFAULTS[key]
    return DEFAULTS[key] if value is None else str(value)


def _on(value) -> bool:
    return str(value).strip().lower() == "true"


def read_config(settings) -> dict:
    """Every notification setting as stored (secrets decrypted - SettingsDB.read does that)."""
    return {k: _read(settings, k) for k in DEFAULTS}


def public_config(settings) -> dict:
    """What GET /api/notifications/settings returns: the settings with each secret replaced by
    a mask when set ("" when not) - never the secret itself."""
    conf = read_config(settings)
    for k in SECRETS:
        conf[k] = MASK if conf[k] else ""
    conf["channels_ready"] = channels(read_config(settings))
    return conf


def channels(conf: dict) -> list[str]:
    """The channels switched on AND complete enough to try."""
    out = []
    if (_on(conf.get("notify_email_enabled")) and conf.get("notify_smtp_host") and conf.get("notify_email_to")
            and (conf.get("notify_email_from") or conf.get("notify_smtp_username"))):
        out.append("email")
    if (_on(conf.get("notify_telegram_enabled")) and conf.get("notify_telegram_bot_token")
            and conf.get("notify_telegram_chat_id")):
        out.append("telegram")
    return out


def _bool(key, value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower()
    raise ValueError(f"{key} must be true or false")


def _text(key, value, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    value = value.strip()
    if len(value) > limit or _CONTROL.search(value):
        raise ValueError(f"{key} is not valid")
    return value


def clean_updates(data, current: dict | None = None) -> dict:
    """Validate a change to the notification settings. Returns {key: value to store}; a secret
    left blank or still masked is not in it (it stays as saved), null clears it. Keys may be
    given with or without the "notify_" prefix. ValueError says what is wrong; nothing is
    returned unless everything is valid."""
    if not isinstance(data, dict) or not data:
        raise ValueError("Expected an object of settings to change")
    out = {}
    for raw_key, value in data.items():
        key = raw_key if str(raw_key).startswith(PREFIX) else PREFIX + str(raw_key)
        if key not in DEFAULTS:
            raise ValueError(f"Unknown notification setting: {raw_key}")
        if key in SECRETS:
            if value is None:
                out[key] = ""
                continue
            value = _text(key, value, 200)
            if not value or value.startswith("••••"):
                continue                                         # blank / still masked: leave it alone
            if key == "notify_telegram_bot_token" and not _TOKEN.match(value):
                raise ValueError("That doesn't look like a Telegram bot token (123456:ABC...)")
            out[key] = value
            continue
        if value is None:
            value = ""
        if key.endswith("_enabled") or key.startswith("notify_event_"):
            out[key] = _bool(key, value)
        elif key == "notify_smtp_host":
            host = _text(key, value, 253)
            out[key] = validate_smtp_host(host) if host else ""
        elif key == "notify_smtp_port":
            try:
                port = int(str(value).strip())
            except ValueError:
                port = 0
            if isinstance(value, bool) or not 1 <= port <= 65535:
                raise ValueError("notify_smtp_port must be a port number, 1-65535")
            out[key] = str(port)
        elif key == "notify_smtp_security":
            sec = _text(key, value, 10).lower()
            if sec not in SECURITY:
                raise ValueError(f"notify_smtp_security must be one of {', '.join(SECURITY)}")
            out[key] = sec
        elif key in ("notify_email_from", "notify_email_to"):
            addr = _text(key, value, 254)
            if addr and not _EMAIL.match(addr):
                raise ValueError(f"{key} must be one e-mail address")
            out[key] = addr
        elif key == "notify_smtp_username":
            out[key] = _text(key, value, 254)
        elif key == "notify_telegram_chat_id":
            chat = _text(key, value, 40)
            if chat and not _CHAT.match(chat):
                raise ValueError("notify_telegram_chat_id must be a numeric chat id or an @channel name")
            out[key] = chat
        elif key in ("notify_quiet_start", "notify_quiet_end"):
            hhmm = _text(key, value, 5)
            if hhmm and not _HHMM.match(hhmm):
                raise ValueError(f"{key} must be a time such as 22:00")
            out[key] = hhmm
    merged = {**(current or {}), **out}
    if bool(merged.get("notify_quiet_start")) != bool(merged.get("notify_quiet_end")):
        raise ValueError("Give both quiet-hours times, or neither")
    return out


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    """Is `now` inside start..end ("HH:MM", local)? A window that wraps midnight (22:00-07:00)
    works; empty or equal times mean no quiet hours."""
    if not (_HHMM.match(start or "") and _HHMM.match(end or "")) or start == end:
        return False
    s, e = time.fromisoformat(start), time.fromisoformat(end)
    t = now.time()
    return s <= t < e if s < e else (t >= s or t < e)


# ── transports ────────────────────────────────────────────────────

def send_email(conf: dict, subject: str, text: str) -> None:
    host = validate_smtp_host(conf["notify_smtp_host"])            # again: the stored value is checked at use
    port = int(conf.get("notify_smtp_port") or 587)
    security = conf.get("notify_smtp_security") or "starttls"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = conf.get("notify_email_from") or conf.get("notify_smtp_username")
    msg["To"] = conf["notify_email_to"]
    msg.set_content(text)
    context = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=SEND_TIMEOUT, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=SEND_TIMEOUT)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if conf.get("notify_smtp_username"):
            server.login(conf["notify_smtp_username"], conf.get("notify_smtp_password") or "")
        server.send_message(msg)


def send_telegram(conf: dict, subject: str, text: str) -> None:
    import requests
    token, chat = conf["notify_telegram_bot_token"], conf["notify_telegram_chat_id"]
    if not _TOKEN.match(token or "") or not _CHAT.match(chat or ""):
        raise DeliveryError("the bot token or chat id is not valid")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": f"{subject}\n\n{text}"[:4000],
                            "disable_web_page_preview": True},
                      timeout=SEND_TIMEOUT)
    if r.status_code != 200:
        raise DeliveryError(f"Telegram answered HTTP {r.status_code}")


TRANSPORTS = {"email": send_email, "telegram": send_telegram}


def deliver(conf: dict, chans: list[str], subject: str, text: str, transports=None) -> list[str]:
    """Send to each channel; returns those that worked. Failures are logged (type only)."""
    transports = transports or TRANSPORTS
    ok = []
    for ch in chans:
        try:
            transports[ch](conf, subject, text)
            ok.append(ch)
        except Exception as e:
            logger.warning("[Notify] %s delivery failed: %s", ch, type(e).__name__)
    return ok


def test_channel(channel: str, settings, overrides: dict | None = None, transports=None) -> None:
    """Send a test message on one channel with the saved settings, overlaid by `overrides`
    (unsaved values from the form, cleaned like a save; a blank/masked secret means the saved
    one). NotifyError with a safe message if it can't be sent."""
    if channel not in TRANSPORTS:
        raise NotifyError("Unknown channel")
    conf = read_config(settings)
    if overrides:
        try:
            conf.update(clean_updates(overrides, conf))
        except ValueError as e:
            raise NotifyError(str(e)) from None
    conf[f"notify_{channel}_enabled"] = "true"
    if channel not in channels(conf):
        raise NotifyError("Fill in the e-mail settings (host, to, and from or user name) first" if channel == "email"
                          else "Fill in the bot token and chat id first")
    try:
        (transports or TRANSPORTS)[channel](conf, "Apex Job Agent: test message",
                                            "Notifications from Apex Job Agent reach you here.")
    except Exception as e:
        logger.warning("[Notify] test %s message failed: %s", channel, type(e).__name__)
        raise NotifyError(f"Could not send the test message ({type(e).__name__})") from None


# ── the notifier ──────────────────────────────────────────────────

def _job_line(job: dict) -> str:
    return f"{job.get('title') or 'A job'} at {job.get('company') or 'a company'}"


def compose(kind: str, lines: list[str]) -> tuple[str, str]:
    n = len(lines)
    subject = "Apex: " + SUBJECTS[kind].format(n=n, s="" if n == 1 else "s", v="s" if n == 1 else "")
    body = [f"- {line}" for line in lines[:MAX_LINES]]
    if n > MAX_LINES:
        body.append(f"...and {n - MAX_LINES} more")
    body += ["", "Open Apex Job Agent to act on these."]
    return subject, "\n".join(body)


class Notifier:
    """The owner's notifications: what to queue, and the one-message-per-kind flush."""

    def __init__(self, path=None, *, settings=None, clock=None, transports=None, user_id: int = OWNER_ID):
        from core.config import cfg
        self.path = path or cfg.JOBS_DB_PATH
        self.user_id = user_id
        self._settings = settings
        self._clock = clock or datetime.now
        self._transports = transports

    def settings(self):
        if self._settings is not None:
            return self._settings
        from db.settings_db import SettingsDB
        return SettingsDB.get()

    def store(self) -> NotificationStore:
        return NotificationStore(self.user_id, self.path)

    def wanted(self, kind: str, conf: dict | None = None) -> bool:
        conf = conf or read_config(self.settings())
        return _on(conf[f"notify_event_{kind}"]) and bool(channels(conf))

    def enqueue(self, kind: str, item_key: str, text: str) -> bool:
        """Queue one line, if that event is on and a channel is set up. Never raises."""
        try:
            if not self.wanted(kind):
                return False
            return self.store().enqueue(kind, item_key, text, self._clock())
        except Exception as e:
            logger.warning("[Notify] could not queue a %s notification: %s", kind, type(e).__name__)
            return False

    # what the stage listener sees

    def on_stage_change(self, user_id, event: dict) -> bool:
        if user_id != self.user_id:
            return False                                  # only the owner's own cards
        to = event.get("to_stage")
        if to not in ("needs_you", "ready_for_review", "submitted") or event.get("kind") != "transition":
            return False
        from db.jobs_db import JobsDB
        job = JobsDB(path=self.path, user_id=user_id).get_application(event["job_id"])
        if job is None:
            return False
        if to == "needs_you":
            reason = (job.get("blocked_reason") or "other").replace("_", " ")
            return self.enqueue("needs_you", f"event:{event['id']}", f"{_job_line(job)} needs you ({reason})")
        if to == "ready_for_review":
            return self.enqueue("ready_for_review", f"event:{event['id']}", f"{_job_line(job)} is ready for review")
        from applier.autonomy import auto_submitted
        if auto_submitted(job.get("events") or [], event["id"]):
            return self.enqueue("autonomy_submitted", f"event:{event['id']}",
                                f"{_job_line(job)} was submitted by an autonomy rule")
        return False

    # the periodic checks

    def check_follow_ups(self, now: datetime) -> int:
        from db.follow_ups_db import FollowUpStore
        n = 0
        for f in FollowUpStore(self.user_id, self.path, lambda: now).due(now):
            when = f["due_at"] if f["status"] == "pending" else f["snoozed_until"]
            what = f["kind"].replace("_", " ")
            n += self.enqueue("follow_up_due", f"{f['id']}:{f['status']}:{when}",
                              f"Follow-up due ({what}): {_job_line(f)}")
        return n

    def check_interviews(self, now: datetime) -> int:
        from db.jobs_db import JobsDB
        n = 0
        apps = JobsDB(path=self.path, user_id=self.user_id, clock=lambda: now).list_applications(
            stage=",".join(INTERVIEW_STAGES))
        for a in apps:
            at = a.get("next_step_at") or ""
            when = hunt.parse_when(at)
            if when is None:
                continue
            if hunt.is_date_only(at):                     # a date alone: that whole day
                soon = when <= now + INTERVIEW_WINDOW and hunt.next_step_due(at) > now
            else:
                soon = now <= when <= now + INTERVIEW_WINDOW
            if soon:
                step = a.get("next_step_text") or "Next step"
                n += self.enqueue("interview_soon", f"{a['job_id']}:{at}", f"{step} - {_job_line(a)} ({at})")
        return n

    # sending

    def flush(self, now: datetime | None = None) -> list[str]:
        """Send what may go now: one message per kind, at most one per kind per RATE_LIMIT,
        ready_for_review only once its oldest line has waited DIGEST_WINDOW, nothing in quiet
        hours. Returns the kinds a message was sent (or attempted) for. Never raises."""
        now = now or self._clock()
        sent = []
        try:
            conf = read_config(self.settings())
            if in_quiet_hours(now, conf["notify_quiet_start"], conf["notify_quiet_end"]):
                return sent
            chans = channels(conf)
            store = self.store()
            for kind in KINDS:
                items = store.pending(kind)
                if not items:
                    continue
                if not _on(conf[f"notify_event_{kind}"]):
                    store.mark([i["id"] for i in items], "dropped", now)      # switched off since: never tell
                    continue
                if not chans:
                    continue
                last = hunt.parse_when(store.last_sent(kind))
                if last is not None and now - last < RATE_LIMIT:
                    continue
                if kind == "ready_for_review":
                    oldest = hunt.parse_when(items[0]["created_at"])
                    if oldest is not None and now - oldest < DIGEST_WINDOW:
                        continue
                subject, text = compose(kind, [i["text"] for i in items])
                ok = deliver(conf, chans, subject, text, self._transports)
                store.mark([i["id"] for i in items], "sent" if ok else "failed", now)
                store.set_last_sent(kind, now)
                sent.append(kind)
        except Exception as e:
            logger.warning("[Notify] flush failed: %s", type(e).__name__)
        return sent

    def tick(self) -> dict:
        """One round: the periodic checks, then the flush. Never raises."""
        now = self._clock()
        found = {}
        for name, check in (("follow_up_due", self.check_follow_ups), ("interview_soon", self.check_interviews)):
            try:
                found[name] = check(now)
            except Exception as e:
                logger.warning("[Notify] %s check failed: %s", name, type(e).__name__)
                found[name] = 0
        return {"queued": found, "sent": self.flush(now)}


# ── the background loop ───────────────────────────────────────────

class NotificationWorker:
    """tick() every `interval` seconds on a daemon thread; wake() makes it look now."""

    def __init__(self, notifier_factory=None, interval: float = TICK_SECONDS, stop_event=None):
        from core.shutdown import shutdown_event
        self._factory = notifier_factory or Notifier
        self._interval = interval
        self._stop_event = stop_event or shutdown_event
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    def wake(self):
        self._wake.set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="notifier", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self):
        while not (self._stop.is_set() or self._stop_event.is_set()):
            try:
                self._factory().tick()
            except Exception as e:
                logger.warning("[Notify] tick failed: %s", type(e).__name__)
            self._wake.wait(self._interval)
            self._wake.clear()


_worker: NotificationWorker | None = None


def on_stage_change(path, user_id, event: dict) -> None:
    """The JobsDB stage listener (registered by start_background)."""
    if Notifier(path).on_stage_change(user_id, event) and _worker is not None:
        _worker.wake()


def start_background() -> None:
    """Called once when the app starts: listen for stage changes and run the periodic loop."""
    global _worker
    from db import jobs_db
    jobs_db.add_stage_listener(on_stage_change)
    if _worker is None:
        _worker = NotificationWorker()
    _worker.start()
