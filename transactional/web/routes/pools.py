"""Lead Pools — large reusable lead lists with dedup and shuffle."""
import re
from html import escape
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List as TList

router = APIRouter()
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


@router.get("/pools", response_class=HTMLResponse)
async def pools_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    pools = []
    for p in db.get_pools(uid):
        pd = dict(p)
        pd["stats"] = db.pool_stats(p["id"])
        pools.append(pd)
    return request.app.state.templates.TemplateResponse(request, "pools.html", {
        "active": "pools", "pools": pools, "db": db,
    })


@router.post("/pools/create")
async def create_pool(request: Request, name: str = Form("")):
    if name.strip():
        uid = request.state.user["id"]
        request.app.state.db.create_pool(name.strip(), uid)
    return RedirectResponse("/pools", status_code=303)


@router.post("/pools/{pid}/import-text", response_class=HTMLResponse)
async def import_text(request: Request, pid: int, leads_text: str = Form("")):
    db = request.app.state.db
    emails = [e.lower() for e in EMAIL_RE.findall(leads_text)]
    result = db.import_pool_leads(pid, emails)
    return HTMLResponse(
        f'<div class="alert alert-success">{result["added"]} added, '
        f'{result["dupes"]} duplicates skipped. '
        f'<a href="/pools" style="color:var(--accent)">Reload</a></div>')


@router.post("/pools/{pid}/upload")
async def upload_pool(request: Request, pid: int,
                      files: TList[UploadFile] = File(...)):
    db = request.app.state.db
    total_added = 0
    total_dupes = 0
    for f in files:
        content = (await f.read()).decode("utf-8", errors="replace")
        emails = [e.lower() for e in EMAIL_RE.findall(content)]
        result = db.import_pool_leads(pid, emails)
        total_added += result["added"]
        total_dupes += result["dupes"]
    return RedirectResponse("/pools", status_code=303)


@router.post("/pools/{pid}/reset")
async def reset_pool(request: Request, pid: int):
    request.app.state.db.reset_pool(pid)
    return RedirectResponse("/pools", status_code=303)


@router.post("/pools/{pid}/reset-all")
async def reset_all(request: Request, pid: int):
    request.app.state.db.reset_pool_all(pid)
    return RedirectResponse("/pools", status_code=303)


@router.post("/pools/{pid}/delete")
async def delete_pool(request: Request, pid: int):
    request.app.state.db.delete_lead_list(pid)
    return RedirectResponse("/pools", status_code=303)
