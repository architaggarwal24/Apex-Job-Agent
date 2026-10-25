# server/routes/prep.py
"""
server/routes/prep.py — interview / offer prep for a card, and the offers you enter
(rows: db/prep_db.py, writing: intelligence/prep.py).

    GET    /api/applications/{job_id}/prep                       all three kinds, each with its status:
                                                                 ready | pending | generating | failed |
                                                                 not_generated; the offer kind also carries
                                                                 the live comparison table of your offers
    POST   /api/applications/{job_id}/prep/{kind}/regenerate     (re)generate one kind in the background;
                                                                 202 with the artifact, now pending   (owner only)
    GET    /api/offers                                           your offers + their comparison table
    POST   /api/offers                                           enter an offer (CTC components)
    PUT    /api/offers/{offer_id}                                change some of its fields
    DELETE /api/offers/{offer_id}

Everything is per user: each handler builds its stores from the session's user, so another
user's job or offer is simply "not found" (404). Generating spends the owner's LLM calls, so
regenerate is owner-only; other accounts see "not_generated". Offers are your own record and
open to any account. A failed artifact shows only the error TYPE.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.auth import get_current_user_id, require_owner
from core.config import cfg
from db import prep_db
from db.jobs_db import JobsDB
from intelligence import prep

logger = logging.getLogger(__name__)
router = APIRouter()


def _job_or_404(uid: int, job_id: str) -> dict:
    app = JobsDB(path=cfg.JOBS_DB_PATH, user_id=uid).get_application(job_id)
    if app is None:
        raise HTTPException(404, "Application not found")
    return app


def _offers_payload(uid: int) -> dict:
    offers = prep_db.OfferStore(uid, cfg.JOBS_DB_PATH).list()
    return {"offers": offers, "comparison": prep_db.compare_offers(offers)}


@router.get("/api/applications/{job_id}/prep")
async def get_prep(job_id: str, uid: int = Depends(get_current_user_id)):
    app = _job_or_404(uid, job_id)
    artifacts = prep_db.PrepStore(uid, cfg.JOBS_DB_PATH).for_job(job_id)
    artifacts["offer"]["comparison"] = _offers_payload(uid)["comparison"]
    return JSONResponse({"job_id": job_id, "stage": app["stage"], "artifacts": artifacts})


@router.post("/api/applications/{job_id}/prep/{kind}/regenerate")
async def regenerate_prep(job_id: str, kind: str, uid: int = Depends(require_owner)):
    if kind not in prep_db.KINDS:
        raise HTTPException(400, f"kind must be one of {list(prep_db.KINDS)}")
    try:
        row = prep_db.PrepStore(uid, cfg.JOBS_DB_PATH).request(job_id, kind)
    except LookupError:
        raise HTTPException(404, "Application not found")
    prep.start(cfg.JOBS_DB_PATH, uid, job_id, kind)
    return JSONResponse(row, status_code=202)


@router.get("/api/offers")
async def list_offers(uid: int = Depends(get_current_user_id)):
    return JSONResponse(_offers_payload(uid))


@router.post("/api/offers")
async def create_offer(data: dict, uid: int = Depends(get_current_user_id)):
    try:
        return JSONResponse(prep_db.OfferStore(uid, cfg.JOBS_DB_PATH).create(data), status_code=201)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/api/offers/{offer_id}")
async def update_offer(offer_id: int, data: dict, uid: int = Depends(get_current_user_id)):
    try:
        offer = prep_db.OfferStore(uid, cfg.JOBS_DB_PATH).update(offer_id, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if offer is None:
        raise HTTPException(404, "Offer not found")
    return JSONResponse(offer)


@router.delete("/api/offers/{offer_id}")
async def delete_offer(offer_id: int, uid: int = Depends(get_current_user_id)):
    if not prep_db.OfferStore(uid, cfg.JOBS_DB_PATH).delete(offer_id):
        raise HTTPException(404, "Offer not found")
    return JSONResponse({"status": "ok"})
