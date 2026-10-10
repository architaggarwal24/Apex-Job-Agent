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
from core.shutdown import shutdown_event, wait_for_threads

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

    On POSIX this runs in its own process group (start_new_session)
    so _stop_next_dev() can kill the whole tree — npm itself often
    execs into the next process, but not on every npm/OS combination,
    and killing only the immediate PID can otherwise leave an orphaned
    node process running after Ctrl+C. On Windows, CREATE_NEW_PROCESS_GROUP
    plus terminate() is the closest equivalent; if a stray `node` process
    is ever left behind there after a force-exit, Task Manager is the
    fallback — Windows process-tree cleanup from Python is inherently
    less reliable than POSIX process groups.
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
    if _next_process is None or _next_process.poll() is not None:
        return
    try:
        if os.name == "nt":
            _next_process.terminate()
        else:
            os.killpg(os.getpgid(_next_process.pid), signal.SIGTERM)
        _next_process.wait(timeout=5)
    except Exception:
        _next_process.kill()
    _next_process = None


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
    from server.routes.queue import get_worker
    get_worker().start()          # recovers interrupted jobs, then works the queue
    llm = provider_info()
    logger.info(f"Starting Apex Job Agent UI → http://localhost:{cfg.APP_PORT}")
    logger.info(f"LLM: {llm['provider']} / {llm['model']}")
    logger.info(f"Dry run: {cfg.DRY_RUN}")
    logger.info("Press Ctrl+C to stop.")

    config = uvicorn.Config(app, host=cfg.APP_HOST, port=cfg.APP_PORT,
                            log_level="warning")
    server = uvicorn.Server(config)
    # Let our custom SIGINT handler take precedence over uvicorn's default
    server.install_signal_handlers = lambda: None
    server.run()


def run_pipeline(quick: bool = False):
    from tracking.pipeline import run
    logger.info(f"Running pipeline (quick={quick})")
    stats = run(quick_mode=quick, cancel_event=shutdown_event)
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
