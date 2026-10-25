# tests/test_shutdown.py
"""
tests/test_shutdown.py — one Ctrl+C stops the app, and a wrong encryption key is explained once.

  * core.shutdown.make_server(): uvicorn no longer takes Ctrl+C away from main.py's handler
    (uvicorn >= 0.29 replaced it and then waited forever for the live-updates stream);
  * core.shutdown.kill_process_tree(): the Next dev server's whole tree (npm -> node) ends,
    not just the npm wrapper;
  * end to end (POSIX only - Windows has no SIGINT to send to a child): a real `python main.py`,
    run from a temporary copy of the backend so it never touches data/ or logs/, exits on ONE
    SIGINT while a signed-in /api/events/stream connection is open;
  * core.crypto: a value encrypted with another key logs one explanation (naming the cause,
    never the key) instead of an error per read; SettingsDB.unreadable_secrets() lists them.

No network beyond 127.0.0.1, no LLM.
"""

import http.client
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
GROUP = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
         else {"start_new_session": True})


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out.split()
    try:
        state = Path(f"/proc/{pid}/status").read_text()
        return "\nState:\tZ" not in state                    # a zombie has already exited
    except FileNotFoundError:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False


class MakeServerTests(unittest.TestCase):
    def test_uvicorn_leaves_the_processs_ctrl_c_handler_in_place(self):
        import uvicorn
        from core.shutdown import make_server

        def mine(signum, frame):
            pass

        old = signal.signal(signal.SIGINT, mine)
        try:
            server = make_server(uvicorn.Config(lambda scope, receive, send: None))
            with server.capture_signals():
                self.assertIs(signal.getsignal(signal.SIGINT), mine)
            server.install_signal_handlers()                  # older uvicorn's hook: a no-op too
            self.assertIs(signal.getsignal(signal.SIGINT), mine)
        finally:
            signal.signal(signal.SIGINT, old)


class KillProcessTreeTests(unittest.TestCase):
    def test_the_child_and_what_it_started_both_end(self):
        from core.shutdown import kill_process_tree
        script = ("import subprocess, sys, time\n"
                  "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                  "print(g.pid, flush=True)\n"
                  "time.sleep(120)\n")
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, **GROUP)
        grandchild = int(proc.stdout.readline())
        try:
            self.assertTrue(_alive(grandchild))
            kill_process_tree(proc, timeout=10)
            self.assertIsNotNone(proc.poll())
            deadline = time.time() + 10
            while _alive(grandchild) and time.time() < deadline:
                time.sleep(0.1)
            self.assertFalse(_alive(grandchild), "the grandchild (node, behind npm) was left running")
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_nothing_to_do_for_no_process_or_one_that_already_ended(self):
        from core.shutdown import kill_process_tree
        kill_process_tree(None)
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        kill_process_tree(proc)                               # must not raise


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(os.name == "posix", "sends SIGINT to a child process")
class OneCtrlCStopsTheServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "app"
        ignore = shutil.ignore_patterns("__pycache__")
        for d in ("applier", "core", "db", "intelligence", "runner", "scraper", "server", "tracking"):
            shutil.copytree(ROOT / d, self.dir / d, ignore=ignore)
        shutil.copy2(ROOT / "main.py", self.dir / "main.py")
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("APEX_", "APP_"))}
        env.update(APP_PORT=str(self.port), APP_HOST="127.0.0.1", ALLOW_SIGNUP="true", COOKIE_SECURE="false",
                   APP_SECRET_KEY="shutdown-test-" + "x" * 40, APP_ENCRYPTION_KEY=Fernet.generate_key().decode(),
                   PYTHONDONTWRITEBYTECODE="1")
        self.log = open(Path(self.tmp.name) / "server.log", "w+")
        self.proc = subprocess.Popen([sys.executable, "main.py"], cwd=self.dir, env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.log.close()
        self.tmp.cleanup()

    def _wait_for_port(self):
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                break
            try:
                with closing(socket.create_connection(("127.0.0.1", self.port), timeout=1)):
                    return
            except OSError:
                time.sleep(0.2)
        self.log.seek(0)
        self.fail("server did not start:\n" + self.log.read()[-3000:])

    def test_one_sigint_exits_while_the_live_stream_is_open(self):
        self._wait_for_port()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/api/auth/signup", json.dumps({"email": "o@example.com", "password": "correct-horse-battery"}),
                  {"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 200, r.read())
        cookie = r.getheader("set-cookie").split(";")[0]
        r.read()
        c.close()

        stream = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        stream.request("GET", "/api/events/stream", headers={"Cookie": cookie})
        resp = stream.getresponse()
        self.assertEqual(resp.status, 200)
        def hold():                                                 # held open, like a browser tab
            try:
                resp.read()
            except (OSError, http.client.HTTPException):
                pass                                                # the server going away ends it
        threading.Thread(target=hold, daemon=True).start()
        time.sleep(0.5)

        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("one Ctrl+C did not stop the server while a live stream was open")
        finally:
            stream.close()
        self.log.seek(0)
        self.assertIn("[Shutdown] Exiting.", self.log.read())


class WrongKeyTests(unittest.TestCase):
    def setUp(self):
        from core import crypto
        self.crypto = crypto
        self.tmp = tempfile.TemporaryDirectory()
        self._old = (os.environ.get("APP_ENCRYPTION_KEY"), crypto._fernet, crypto._KEY_PATH,
                     crypto._reported_unreadable)
        crypto._KEY_PATH = Path(self.tmp.name) / ".master.key"
        self.file_key = Fernet.generate_key().decode()
        crypto._KEY_PATH.write_text(self.file_key)
        os.environ.pop("APP_ENCRYPTION_KEY", None)
        crypto._fernet = None
        crypto._reported_unreadable = False
        self.saved = crypto.encrypt("sk-live-secret")       # saved with the auto-generated file key
        self.env_key = Fernet.generate_key().decode()
        os.environ["APP_ENCRYPTION_KEY"] = self.env_key    # ...then APP_ENCRYPTION_KEY was set
        crypto._fernet = None

    def tearDown(self):
        old_env, crypto_fernet, key_path, reported = self._old
        if old_env is None:
            os.environ.pop("APP_ENCRYPTION_KEY", None)
        else:
            os.environ["APP_ENCRYPTION_KEY"] = old_env
        self.crypto._fernet, self.crypto._KEY_PATH, self.crypto._reported_unreadable = crypto_fernet, key_path, reported
        self.tmp.cleanup()

    def test_explained_once_with_the_cause_and_never_the_key(self):
        with self.assertLogs("core.crypto", level="DEBUG") as logs:
            for _ in range(5):
                self.assertEqual(self.crypto.decrypt(self.saved), "")
        errors = [r for r in logs.records if r.levelno >= logging.ERROR]
        self.assertEqual(len(errors), 1)
        text = errors[0].getMessage()
        self.assertIn("APP_ENCRYPTION_KEY is set and differs from", text)
        self.assertIn(".master.key", text)
        for secret in (self.env_key, self.file_key, "sk-live-secret"):
            self.assertNotIn(secret, "\n".join(logs.output))

    def test_hint_without_the_env_key_points_at_the_key_file(self):
        os.environ.pop("APP_ENCRYPTION_KEY", None)
        self.assertIn("is not the one these values were encrypted with", self.crypto.key_hint())

    def test_restoring_the_file_key_reads_the_value_again(self):
        self.assertFalse(self.crypto.can_decrypt(self.saved))
        os.environ["APP_ENCRYPTION_KEY"] = self.file_key
        self.crypto._fernet = None
        self.assertTrue(self.crypto.can_decrypt(self.saved))
        self.assertEqual(self.crypto.decrypt(self.saved), "sk-live-secret")
        self.assertTrue(self.crypto.can_decrypt("plain") and self.crypto.can_decrypt(""))

    def test_settings_lists_the_unreadable_secrets_by_name(self):
        from db.settings_db import SettingsDB
        db = SettingsDB(path=Path(self.tmp.name) / "settings.db")
        db.write("openai_api_key", "sk-readable")                 # current key: fine
        db.write("notify_smtp_password", "pw")
        with closing(__import__("sqlite3").connect(str(db.path))) as c:   # one saved under the old key
            c.execute("UPDATE settings SET value=? WHERE key='notify_smtp_password'", (self.saved,))
            c.commit()
        self.assertEqual(db.unreadable_secrets(), ["notify_smtp_password"])


if __name__ == "__main__":
    unittest.main()
