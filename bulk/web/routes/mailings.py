"""Mailings page — list, add, edit, start, stop, delete."""
import json
import time
import threading
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()
_cores = {}
_threads = {}


@router.get("/mailings", response_class=HTMLResponse)
async def mailings_page(request: Request):
    db = request.app.state.db
    tpl = request.app.state.templates
    rows = db.get_mailings()
    mailings = []
    for r in rows:
        m = dict(r)
        total = m.get("total_leads", 0) or 0
        sent = m.get("sent", 0) or 0
        failed = m.get("failed", 0) or 0
        m["pct"] = int((sent + failed) / total * 100) if total > 0 else 0
        m["running"] = m["id"] in _cores
        mailings.append(m)
    return tpl.TemplateResponse("mailings.html", {
        "request": request, "active": "mailings", "mailings": mailings,
        "brands": db.get_brands(), "domains": db.get_domains(),
        "lists": db.get_lists(), "smtps": db.get_smtps(),
        "templates": db.get_templates(), "db": db,
    })


@router.post("/mailings/add")
async def add_mailing(request: Request,
                       name: str = Form(""), brand_id: int = Form(0),
                       domain_id: int = Form(0), list_id: int = Form(0),
                       smtp_preset_id: int = Form(0), template_id: int = Form(0),
                       daily_limit: int = Form(0), exclude_domains: str = Form(""),
                       test_email: str = Form(""), test_interval: int = Form(0),
                       schedule_time: str = Form("")):
    db = request.app.state.db
    excludes = [d.strip() for d in exclude_domains.split(",") if d.strip()]
    db.create_mailing(
        name=name or f"Mailing {time.strftime('%Y-%m-%d %H:%M')}",
        brand_id=brand_id, domain_id=domain_id, list_id=list_id,
        smtp_preset_id=smtp_preset_id, template_id=template_id,
        daily_limit=daily_limit, exclude_domains_json=json.dumps(excludes),
        total_leads=db.get_list_lead_count(list_id),
        test_email=test_email, test_interval=test_interval,
        schedule_time=schedule_time,
    )
    return RedirectResponse("/mailings", status_code=303)


@router.post("/mailings/{mid}/start")
async def start_mailing(request: Request, mid: int):
    db = request.app.state.db
    if mid in _cores:
        return RedirectResponse("/mailings", status_code=303)

    from bulk.mailer.bulk_core import BulkMailerCore
    core = BulkMailerCore(db, mid)
    _cores[mid] = core

    def run():
        try:
            row = db._conn().execute("SELECT schedule_time FROM mailings WHERE id=?", (mid,)).fetchone()
            schedule = row["schedule_time"] if row else ""
            if schedule:
                from datetime import datetime, timedelta
                try:
                    target_time = datetime.strptime(schedule, "%H:%M").time()
                    now = datetime.now()
                    target = datetime.combine(now.date(), target_time)
                    if target <= now:
                        target += timedelta(days=1)
                    wait = (target - now).total_seconds()
                    while wait > 0 and mid in _cores:
                        time.sleep(min(wait, 5))
                        wait = (target - datetime.now()).total_seconds()
                except ValueError:
                    pass
            core.run()
        except Exception as e:
            import logging
            logging.getLogger("bulk.web").error("Mailing %d error: %s", mid, e)
        finally:
            _cores.pop(mid, None)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    _threads[mid] = t
    return RedirectResponse("/mailings", status_code=303)


@router.post("/mailings/{mid}/stop")
async def stop_mailing(request: Request, mid: int):
    core = _cores.get(mid)
    if core:
        core.stop()
        _cores.pop(mid, None)
    return RedirectResponse("/mailings", status_code=303)


@router.post("/mailings/{mid}/delete")
async def delete_mailing(request: Request, mid: int):
    if mid in _cores:
        return RedirectResponse("/mailings", status_code=303)
    db = request.app.state.db
    c = db._conn()
    c.execute("DELETE FROM mailings WHERE id=?", (mid,))
    c.commit()
    return RedirectResponse("/mailings", status_code=303)


@router.get("/mailings/{mid}/stats", response_class=HTMLResponse)
async def mailing_stats(request: Request, mid: int):
    """HTMX polling endpoint for live stats."""
    db = request.app.state.db
    m = db._conn().execute("SELECT * FROM mailings WHERE id=?", (mid,)).fetchone()
    if not m:
        return "<div>Not found</div>"
    md = dict(m)
    total = md.get("total_leads", 0) or 0
    sent = md.get("sent", 0) or 0
    failed = md.get("failed", 0) or 0
    pct = int((sent + failed) / total * 100) if total > 0 else 0
    running = mid in _cores
    return f"""
    <div class="grid-3" style="margin-bottom:15px">
        <div class="metric"><div class="value">{sent:,}</div><div class="label">Sent</div></div>
        <div class="metric"><div class="value">{failed:,}</div><div class="label">Failed</div></div>
        <div class="metric"><div class="value">{total-sent-failed:,}</div><div class="label">Remaining</div></div>
    </div>
    <div class="progress"><div class="progress-bar" style="width:{pct}%">{pct}%</div></div>
    <p style="margin-top:8px;font-size:12px;color:var(--fg2)">
        Status: <span class="badge badge-{'running' if running else md.get('status','draft').lower()}">{md.get('status','DRAFT')}</span>
    </p>
    """
