"""
main.py — Apex Job Agent entry point.

Usage:
  python main.py              → start web UI (default)
  python main.py --pipeline   → run search pipeline once, then exit
  python main.py --quick      → quick pipeline (2 titles × 2 locations)
  python main.py --schedule   → web UI + daily scheduled pipeline
  python main.py --parse      → parse resume.pdf and seed profile DB
  python main.py --dev-ui     → web UI + Next.js dev server (hot reload),
                                 both in this one terminal

For everyday use (no frontend editing): `cd frontend && npm run build`
once, then just `python main.py` — one command, one terminal. The build
is a static export that this Python server serves itself at "/".
--dev-ui is only for when you're actively changing frontend code and
want changes to appear without rebuilding; use http://localhost:3000.

Press Ctrl+C to stop. The agent will:
  1. Signal any running pipeline/scoring batch to stop ASAP
  2. Wait briefly for it to wind down
  3. Exit cleanly
Press Ctrl+C twice to force-exit immediately.
"""

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from core.config   import cfg
from core.shutdown import kill_process_tree, make_server, shutdown_event, wait_for_threads

cfg.ensure_dirs()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(
            cfg.LOGS_DIR / f"apex_{date.today().isoformat()}.log",
            encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")

_force_exit_count = 0
_next_process = None   # set by _start_next_dev(), read by the signal handler


def _signal_handler(signum, frame):
    """
    Ctrl+C handler.
    First press: set shutdown_event so any running pipeline/scoring
    batch stops as soon as it checks the flag, then exit normally.
    Second press: force-exit immediately, no cleanup.
    """
    global _force_exit_count
    _force_exit_count += 1

    if _force_exit_count == 1:
        print()  # newline after ^C
        logger.info("[Shutdown] Ctrl+C received — stopping background tasks "
                    "(press Ctrl+C again to force-exit immediately)")
        _stop_next_dev()
        shutdown_event.set()
        wait_for_threads(timeout=8.0)
        logger.info("[Shutdown] Exiting.")
        os._exit(0)
    else:
        print()
        logger.warning("[Shutdown] Force-exit requested.")
        _stop_next_dev()
        os._exit(1)


def _start_next_dev():
    """
    Launches `npm run dev` (the Next.js dev server, port 3000, hot reload)
    as a child process so --dev-ui needs only this one terminal. Its
    output is piped into our own logger with a [next] prefix rather
    than left to print to a second window.

    It runs in its own process group (start_new_session on POSIX,
    CREATE_NEW_PROCESS_GROUP on Windows, which also keeps Ctrl+C from
    reaching it directly) so _stop_next_dev() can end the whole tree —
    npm → node — with core.shutdown.kill_process_tree(); killing only the
    immediate PID would leave node running and holding port 3000.
    """
    global _next_process
    frontend_dir = ROOT / "frontend"
    npm = shutil.which("npm")

    if not npm:
        logger.warning("[Next] npm not found on PATH — skipping dev server. "
                       "Install Node.js, or drop --dev-ui and use "
                       "`npm run build` + plain `python main.py` instead.")
        return
    if not (frontend_dir / "node_modules").exists():
        logger.warning(f"[Next] {frontend_dir}/node_modules missing — run "
                       "`npm install` in frontend/ once, then retry --dev-ui.")
        return

    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    _next_process = subprocess.Popen(
        [npm, "run", "dev"], cwd=str(frontend_dir),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1, **kwargs,
    )

    def _pipe_output():
        for line in _next_process.stdout:
            print(f"[next] {line.rstrip()}")

    threading.Thread(target=_pipe_output, daemon=True).start()
    # Tells create_app() (server/app.py) to redirect the app's pages to the
    # dev server when there's no production build to serve. Must be set
    # before create_app() runs — start_server() guarantees that ordering.
    os.environ["APEX_DEV_UI_ORIGIN"] = "http://localhost:3000"
    logger.info("[Next] Dev server starting → http://localhost:3000 "
               "(open THIS one; edits to frontend/src hot-reload there)")


def _stop_next_dev():
    global _next_process
    proc, _next_process = _next_process, None
    kill_process_tree(proc, timeout=4.0)


def _check_encryption_key():
    """One clear startup warning, instead of an error on every settings read, when saved
    secrets can't be decrypted - usually APP_ENCRYPTION_KEY set after they were saved."""
    from core.crypto import key_hint
    from db.settings_db import SettingsDB
    bad = SettingsDB.get().unreadable_secrets()
    if bad:
        logger.warning(f"[Crypto] {len(bad)} saved secret(s) can't be decrypted and read as empty: "
                       f"{', '.join(bad)}. {key_hint()}")


def start_server(dev_ui: bool = False):
    import uvicorn
    from server.app import create_app
    from core.llm import provider_info

    if dev_ui:
        _start_next_dev()
    elif (ROOT / "frontend" / "out").exists():
        logger.info(f"UI (built) → http://localhost:{cfg.APP_PORT}/")
    else:
        logger.info("UI not built yet — run `cd frontend && npm install && npm run build` "
                    "once, or use `python main.py --dev-ui` for the hot-reload dev server.")

    app = create_app()
    _check_encryption_key()
    from server.routes.queue import get_worker
    get_worker().start()          # recovers interrupted jobs, then works the queue
    from intelligence import prep
    prep.start_background()       # prep for cards entering an interview/offer stage
    from core import notifier
    notifier.start_background()   # e-mail / Telegram when the agent needs you
    llm = provider_info()
    logger.info(f"Starting Apex Job Agent UI → http://localhost:{cfg.APP_PORT}")
    logger.info(f"LLM: {llm['provider']} / {llm['model']}")
    logger.info(f"Dry run: {cfg.DRY_RUN}")
    logger.info("Press Ctrl+C to stop.")

    config = uvicorn.Config(app, host=cfg.APP_HOST, port=cfg.APP_PORT,
                            log_level="warning")
    # main.py's _signal_handler keeps Ctrl+C: uvicorn's own handler would wait forever for
    # the live-updates stream to close (core/shutdown.make_server).
    server = make_server(config)
    server.run()


def run_pipeline(quick: bool = False):
    from tracking.pipeline import run
    logger.info(f"Running pipeline (quick={quick})")
    stats = run(quick_mode=quick, cancel_event=shutdown_event)
    from server.routes.jobs import _mark_search_run
    _mark_search_run()                       # the Today page's get-started checklist
    logger.info(f"Pipeline complete: {stats}")
    return stats


def run_scheduled():
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    schedule_time = os.getenv("SCHEDULE_TIME", "09:00")
    timezone      = os.getenv("TIMEZONE",      "Asia/Kolkata")
    hour, minute  = schedule_time.split(":")

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_pipeline,
        CronTrigger(hour=int(hour), minute=int(minute), timezone=timezone),
        id="daily_pipeline",
    )
    scheduler.start()
    logger.info(f"Scheduled daily pipeline at {schedule_time} {timezone}")
    start_server()


def parse_resume_cmd():
    import json
    from core.resume_parser import parse
    from db.profile_db import ProfileDB

    logger.info("Parsing resume...")
    parsed = parse()
    db = ProfileDB()
    db.import_from_parsed(parsed)
    logger.info(f"Imported: {parsed.get('full_name','?')} | {len(parsed.get('skills',[]))} skills")
    print(json.dumps(
        {k: v for k, v in parsed.items() if k not in ("education","experience","projects")},
        indent=2, ensure_ascii=False
    ))


if __name__ == "__main__":
    # Register Ctrl+C handler before anything else starts
    signal.signal(signal.SIGINT, _signal_handler)
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
    except (AttributeError, ValueError):
        pass  # SIGTERM not available on some platforms

    parser = argparse.ArgumentParser(description="Apex Job Agent")
    parser.add_argument("--pipeline",  action="store_true", help="Run search pipeline once")
    parser.add_argument("--quick",     action="store_true", help="Quick pipeline mode")
    parser.add_argument("--schedule",  action="store_true", help="Web UI + daily schedule")
    parser.add_argument("--parse",     action="store_true", help="Parse resume.pdf")
    parser.add_argument("--dev-ui",    action="store_true",
                        help="Also run the Next.js dev server (hot reload) in this terminal")
    args = parser.parse_args()

    if args.parse:
        parse_resume_cmd()
    elif args.pipeline:
        run_pipeline(quick=args.quick)
    elif args.schedule:
        run_scheduled()
    else:
        start_server(dev_ui=args.dev_ui)
