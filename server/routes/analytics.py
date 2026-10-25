# server/routes/analytics.py
"""
server/routes/analytics.py — how your applications turned out (db/outcomes_db.py).

    GET /api/analytics/outcomes   reply / interview / offer rates and the median days to a
                                  first response, overall and by source, title, score band,
                                  resume variant and apply mode, plus "calibration": a
                                  recommended auto_skip_score_threshold with a one-line reason

Per user: the numbers are built from the session user's own applications only. Read-only:
the recommendation is advice; the threshold itself is still changed in Settings (owner-only).
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.auth import get_current_user_id
from core.config import cfg
from db.outcomes_db import OutcomeStats
from db.settings_db import SettingsDB

router = APIRouter()


@router.get("/api/analytics/outcomes")
async def outcomes(uid: int = Depends(get_current_user_id)):
    threshold = SettingsDB.get().auto_skip_score_threshold()
    return JSONResponse(OutcomeStats(uid, cfg.JOBS_DB_PATH).report(threshold))
