# server/routes/views.py
"""
server/routes/views.py — saved filter views of the Applications page (db/views_db.py).

    GET    /api/views          this user's views [{id, name, query}]
    POST   /api/views          {"name", "query"} add, or replace the one with that name
    DELETE /api/views/{id}     delete one of this user's views

Views are each account's own (any signed-in user, not only the owner): they change
nothing but what that person's page shows.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.auth import get_current_user_id
from db.views_db import ViewStore

router = APIRouter()


@router.get("/api/views")
async def list_views(uid: int = Depends(get_current_user_id)):
    return JSONResponse({"views": ViewStore(uid).list()})


@router.post("/api/views")
async def save_view(data: dict, uid: int = Depends(get_current_user_id)):
    if not isinstance(data, dict):
        raise HTTPException(400, "Body must be {name, query}")
    try:
        return JSONResponse(ViewStore(uid).save(data.get("name"), data.get("query", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/views/{view_id}")
async def delete_view(view_id: int, uid: int = Depends(get_current_user_id)):
    if not ViewStore(uid).delete(view_id):
        raise HTTPException(404, "View not found")
    return JSONResponse({"deleted": view_id})
