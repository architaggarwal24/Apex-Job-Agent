"""
main.py — Apex Job Agent entry point.

Usage:
  python main.py              → start web UI (default)
  python main.py --pipeline   → run search pipeline once, then exit
  python main.py --quick      → quick pipeline (2 titles × 2 locations)
  python main.py --schedule   → web UI + daily scheduled pipeline
  python main.py --parse      → parse resume.pdf and seed profile DB
"""

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from core.config import cfg

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


def start_server():
    import uvicorn
    from server.app import create_app
    from core.llm import provider_info

    app = create_app()
    llm = provider_info()
    logger.info(f"Starting Apex Job Agent UI → http://localhost:{cfg.APP_PORT}")
    logger.info(f"LLM: {llm['provider']} / {llm['model']}")
    logger.info(f"Dry run: {cfg.DRY_RUN}")
    uvicorn.run(app, host=cfg.APP_HOST, port=cfg.APP_PORT, log_level="warning")


def run_pipeline(quick: bool = False):
    from tracking.pipeline import run
    logger.info(f"Running pipeline (quick={quick})")
    stats = run(quick_mode=quick)
    logger.info(f"Pipeline complete: {stats}")
    return stats


def run_scheduled():
    import threading
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
    parser = argparse.ArgumentParser(description="Apex Job Agent")
    parser.add_argument("--pipeline",  action="store_true", help="Run search pipeline once")
    parser.add_argument("--quick",     action="store_true", help="Quick pipeline mode")
    parser.add_argument("--schedule",  action="store_true", help="Web UI + daily schedule")
    parser.add_argument("--parse",     action="store_true", help="Parse resume.pdf")
    args = parser.parse_args()

    if args.parse:
        parse_resume_cmd()
    elif args.pipeline:
        run_pipeline(quick=args.quick)
    elif args.schedule:
        run_scheduled()
    else:
        start_server()
