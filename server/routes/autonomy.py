# server/routes/autonomy.py
"""
server/routes/autonomy.py — what the agent may approve by itself, and the
measured record behind it (applier/autonomy.py has the rules' semantics).

    GET    /api/automation/ledger        per site type: attempts that reached review,
                                         approved unchanged / with edits, rejected,
                                         auto-approved, failures by class, and the
                                         success rate over the last 20 reviewed
    GET    /api/automation/rules         {autonomy_enabled, rules: [...]}
    POST   /api/automation/rules         create a rule        (owner only)
    PUT    /api/automation/rules/{id}    change a rule; {"enabled": true} re-enables
                                         one autonomy switched off  (owner only)
    DELETE /api/automation/rules/{id}    delete a rule        (owner only)
    PUT    /api/automation/autonomy      {"autonomy_enabled": bool}: the kill switch,
                                         OFF until you turn it on  (owner only)

Everything is per user: each handler builds AutonomyStore(uid) from the session,
so someone else's rule or ledger is simply not found. Reading is open to any
signed-in user; changing what the agent may do on its own is owner-only, like
the other automation controls.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.auth import get_current_user_id, require_owner
from db.autonomy_db import WINDOW, AutonomyStore

router = APIRouter()


def _rule_or_404(store: AutonomyStore, rule_id: int) -> dict:
    rule = store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    return rule


@router.get("/api/automation/ledger")
async def get_ledger(uid: int = Depends(get_current_user_id)):
    return JSONResponse({"window": WINDOW, "site_types": AutonomyStore(uid).ledger()})


@router.get("/api/automation/rules")
async def list_rules(uid: int = Depends(get_current_user_id)):
    store = AutonomyStore(uid)
    return JSONResponse({"autonomy_enabled": store.is_enabled(), "rules": store.list_rules()})


@router.post("/api/automation/rules")
async def create_rule(data: dict, uid: int = Depends(require_owner)):
    """Body: site_type (a site type or "*"), min_score and max_per_day (required),
    and optionally require_all_answers_known (default true), min_attempts
    (default 20), min_success_rate (default 0.9). 400 for anything invalid."""
    try:
        rule = AutonomyStore(uid).add_rule(data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse(rule)


@router.put("/api/automation/rules/{rule_id}")
async def update_rule(rule_id: int, data: dict, uid: int = Depends(require_owner)):
    store = AutonomyStore(uid)
    _rule_or_404(store, rule_id)
    try:
        rule = store.update_rule(rule_id, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if rule is None:
        raise HTTPException(404, "Rule not found")
    return JSONResponse(rule)


@router.delete("/api/automation/rules/{rule_id}")
async def delete_rule(rule_id: int, uid: int = Depends(require_owner)):
    if not AutonomyStore(uid).delete_rule(rule_id):
        raise HTTPException(404, "Rule not found")
    return JSONResponse({"status": "deleted"})


@router.put("/api/automation/autonomy")
async def set_autonomy(data: dict, uid: int = Depends(require_owner)):
    value = data.get("autonomy_enabled")
    if not isinstance(value, bool):
        raise HTTPException(400, "autonomy_enabled must be true or false")
    store = AutonomyStore(uid)
    store.set_enabled(value)
    return JSONResponse({"autonomy_enabled": store.is_enabled()})
