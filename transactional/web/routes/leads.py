"""Leads — import, view, bulk upload."""
import re
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List as TList

router = APIRouter()
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


@router.post("/leads/{cid}/upload")
async def upload_leads(request: Request, cid: int,
                       files: TList[UploadFile] = File(...)):
    db = request.app.state.db
    total = 0
    for f in files:
        content = (await f.read()).decode("utf-8", errors="replace")
        emails = EMAIL_RE.findall(content)
        total += db.import_leads(cid, emails)
    db.update_campaign(cid, total_leads=db.get_lead_count(cid))
    return RedirectResponse("/campaigns", status_code=303)


@router.get("/leads/{cid}", response_class=HTMLResponse)
async def view_leads(request: Request, cid: int, q: str = ""):
    db = request.app.state.db
    camp = db.get_campaign(cid)
    if not camp:
        return RedirectResponse("/campaigns", status_code=303)
    camp = dict(camp)
    states = db.get_lead_states(cid)

    if q:
        leads = db._conn().execute(
            "SELECT id, email, state FROM trans_leads WHERE campaign_id=? AND email LIKE ? LIMIT 500",
            (cid, f"%{q}%")).fetchall()
    else:
        leads = db._conn().execute(
            "SELECT id, email, state FROM trans_leads WHERE campaign_id=? LIMIT 500",
            (cid,)).fetchall()
    leads = [dict(l) for l in leads]

    return request.app.state.templates.TemplateResponse(request, "leads.html", {
        "active": "campaigns", "campaign": camp, "leads": leads,
        "states": states, "query": q,
    })
