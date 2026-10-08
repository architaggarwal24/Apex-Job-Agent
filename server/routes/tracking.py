"""
server/routes/tracking.py — Application pipeline tracking + analytics.
"""

import logging
<<<<<<< HEAD
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from db.jobs_db import JobsDB, PIPELINE_STAGES
=======
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.auth import get_current_user_id
from db.jobs_db import JobsDB, PIPELINE_STAGES
from db.lifecycle import IllegalTransition
>>>>>>> 4226f10 (.)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/tracking/pipeline")
<<<<<<< HEAD
async def get_pipeline():
    """Return jobs grouped by pipeline stage for Kanban view."""
    return JSONResponse(JobsDB().get_pipeline())


@router.post("/api/tracking/stage/{job_id}")
async def update_stage(job_id: str, data: dict):
    stage = data.get("stage", "")
    note  = data.get("note",  "")
    if stage not in PIPELINE_STAGES:
        raise HTTPException(400, f"Invalid stage. Valid: {PIPELINE_STAGES}")
    db = JobsDB()
    if not db.get_by_id(job_id):
        raise HTTPException(404, "Job not found")
    db.add_stage(job_id, stage, note)
=======
async def get_pipeline(uid: int = Depends(get_current_user_id)):
    """Return jobs grouped by pipeline stage for Kanban view."""
    return JSONResponse(JobsDB(user_id=uid).get_pipeline())


@router.post("/api/tracking/stage/{job_id}")
async def update_stage(job_id: str, data: dict,
                       uid: int = Depends(get_current_user_id)):
    stage = data.get("stage", "")
    note  = str(data.get("note") or "")
    if stage not in PIPELINE_STAGES:
        raise HTTPException(400, f"Invalid stage. Valid: {PIPELINE_STAGES}")
    db = JobsDB(user_id=uid)
    if not db.get_by_id(job_id):
        raise HTTPException(404, "Job not found")
    try:
        db.add_stage(job_id, stage, note)
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
>>>>>>> 4226f10 (.)
    return JSONResponse({"status": "ok", "stage": stage})


@router.post("/api/tracking/note/{job_id}")
<<<<<<< HEAD
async def add_note(job_id: str, data: dict):
    note = data.get("note", "").strip()
    if not note:
        raise HTTPException(400, "Note cannot be empty")
    db = JobsDB()
=======
async def add_note(job_id: str, data: dict,
                   uid: int = Depends(get_current_user_id)):
    note = data.get("note", "").strip()
    if not note:
        raise HTTPException(400, "Note cannot be empty")
    db = JobsDB(user_id=uid)
>>>>>>> 4226f10 (.)
    if not db.get_by_id(job_id):
        raise HTTPException(404, "Job not found")
    db.add_note(job_id, note)
    return JSONResponse({"status": "ok"})


@router.get("/api/tracking/analytics")
<<<<<<< HEAD
async def get_analytics():
=======
async def get_analytics(uid: int = Depends(get_current_user_id)):
>>>>>>> 4226f10 (.)
    """
    Return analytics data for charts:
    - Applications per day (last 30 days)
    - Stage conversion funnel
    - Top companies
    - Score distribution
<<<<<<< HEAD
    """
    import sqlite3
    from datetime import date, timedelta
    from core.config import cfg

    db_path = str(cfg.JOBS_DB_PATH)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Applications per day (last 30 days)
    daily = []
    for i in range(29, -1, -1):
        day = (date.today() - timedelta(days=i)).isoformat()
        row = conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE found_at LIKE ?",
            (f"{day}%",)
        ).fetchone()
        applied_row = conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE applied_at LIKE ?",
            (f"{day}%",)
        ).fetchone()
        daily.append({
            "date":    day,
            "found":   row["n"],
            "applied": applied_row["n"],
        })

    # Score distribution buckets
    score_dist = []
    for lo, hi in [(0,30),(30,50),(50,70),(70,90),(90,101)]:
        row = conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE ai_score >= ? AND ai_score < ?",
            (lo, hi)
        ).fetchone()
        score_dist.append({"range": f"{lo}-{hi-1}", "count": row["n"]})

    # Top companies by application count
    top_cos = conn.execute("""
        SELECT company, COUNT(*) n FROM jobs
        WHERE status='applied' GROUP BY company
        ORDER BY n DESC LIMIT 10
    """).fetchall()

    # Stage funnel
    funnel_stages = ["applied","recruiter_screen","technical_round",
                     "hiring_manager","offer"]
    funnel = []
    for stage in funnel_stages:
        row = conn.execute("""
            SELECT COUNT(DISTINCT job_id) n FROM job_stages
            WHERE stage=? AND id IN (SELECT MAX(id) FROM job_stages GROUP BY job_id)
        """, (stage,)).fetchone()
        funnel.append({"stage": stage.replace("_"," ").title(), "count": row["n"]})

    # Source breakdown
    sources = conn.execute("""
        SELECT source, COUNT(*) n FROM jobs GROUP BY source
    """).fetchall()

    conn.close()

    return JSONResponse({
        "daily":       daily,
        "score_dist":  score_dist,
        "top_companies": [dict(r) for r in top_cos],
        "funnel":      funnel,
        "sources":     [dict(r) for r in sources],
    })
=======

    Backed by JobsDB.get_analytics(), which uses 6 GROUP BY queries
    instead of the ~72 individual per-day queries this previously ran.
    """
    return JSONResponse(JobsDB(user_id=uid).get_analytics())
>>>>>>> 4226f10 (.)
