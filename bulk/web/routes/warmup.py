"""Warmup — Seed management, campaigns, action log."""
import json
import threading
import time
import logging
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from bulk.mailer.warmup_providers import PROVIDERS, get_curve_values, WARMUP_CURVES

logger = logging.getLogger("bulk.warmup.web")
router = APIRouter()

_engines = {}
_action_thread = None


def _start_action_worker(db):
    """Background thread that periodically executes pending warmup actions."""
    global _action_thread
    if _action_thread and _action_thread.is_alive():
        return
    from bulk.mailer.warmup_engine import WarmupEngine
    engine = WarmupEngine(db)

    def worker():
        while True:
            try:
                n = engine.execute_pending_actions()
                if n > 0:
                    logger.info("Executed %d warmup actions", n)
            except Exception as e:
                logger.error("Action worker error: %s", e)
            time.sleep(30)

    _action_thread = threading.Thread(target=worker, daemon=True)
    _action_thread.start()


@router.get("/warmup", response_class=HTMLResponse)
async def warmup_page(request: Request):
    db = request.app.state.db
    _start_action_worker(db)
    seeds = [dict(s) for s in db.get_seeds()]
    campaigns = [dict(c) for c in db.get_warmup_campaigns()]
    actions = [dict(a) for a in db.get_warmup_log(limit=50)]
    smtps = [dict(s) for s in db.get_smtps()]
    templates = [dict(t) for t in db.get_templates()]
    providers = sorted(PROVIDERS.keys())

    provider_counts = {}
    for s in seeds:
        p = s.get("provider", "")
        provider_counts[p] = provider_counts.get(p, 0) + 1

    llm_cfg = db.get_llm_config()
    return request.app.state.templates.TemplateResponse(request, "warmup.html", {
        "active": "warmup", "seeds": seeds, "campaigns": campaigns,
        "actions": actions, "smtps": smtps, "templates": templates,
        "providers": providers, "provider_counts": provider_counts,
        "curves": list(WARMUP_CURVES.keys()), "db": db,
        "llm": llm_cfg,
    })


# --- Seeds ---

@router.post("/warmup/seeds/add")
async def add_seed(request: Request,
                   provider: str = Form(""),
                   email: str = Form(""),
                   password: str = Form(""),
                   proxy: str = Form("")):
    db = request.app.state.db
    if not email.strip() or not password.strip():
        return RedirectResponse("/warmup", status_code=303)
    cfg = PROVIDERS.get(provider.lower(), {})
    db.add_seed(
        provider=provider.lower(),
        email=email.strip(),
        password=password.strip(),
        imap_host=cfg.get("imap_host", ""),
        imap_port=cfg.get("imap_port", 993),
        smtp_host=cfg.get("smtp_host", ""),
        smtp_port=cfg.get("smtp_port", 587),
        proxy=proxy.strip(),
    )
    return RedirectResponse("/warmup", status_code=303)


@router.post("/warmup/seeds/bulk-add", response_class=HTMLResponse)
async def bulk_add_seeds(request: Request,
                         provider: str = Form(""),
                         accounts: str = Form(""),
                         proxy: str = Form("")):
    """Add multiple seeds at once: one email:password per line."""
    db = request.app.state.db
    cfg = PROVIDERS.get(provider.lower(), {})
    added = 0
    for line in accounts.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        parts = line.split(":", 1)
        em, pw = parts[0].strip(), parts[1].strip()
        if not em or not pw:
            continue
        try:
            db.add_seed(
                provider=provider.lower(), email=em, password=pw,
                imap_host=cfg.get("imap_host", ""),
                imap_port=cfg.get("imap_port", 993),
                smtp_host=cfg.get("smtp_host", ""),
                smtp_port=cfg.get("smtp_port", 587),
                proxy=proxy.strip(),
            )
            added += 1
        except Exception:
            pass
    return HTMLResponse(f'<div class="alert alert-success">{added} seeds added for {provider}</div>')


@router.post("/warmup/seeds/{sid}/delete")
async def delete_seed(request: Request, sid: int):
    request.app.state.db.delete_seed(sid)
    return RedirectResponse("/warmup", status_code=303)


@router.post("/warmup/seeds/{sid}/test", response_class=HTMLResponse)
async def test_seed(request: Request, sid: int):
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM warmup_seeds WHERE id=?", (sid,)).fetchone()
    if not row:
        return HTMLResponse('<span style="color:var(--red)">Not found</span>')
    row = dict(row)

    from bulk.mailer.warmup_imap import IMAPWorker
    worker = IMAPWorker(
        row["email"], row["password"],
        row["imap_host"], row["imap_port"],
        row.get("smtp_host", ""), row.get("smtp_port", 587),
        row.get("proxy", ""), row.get("provider", ""))

    if worker.connect():
        info = worker.check_inbox()
        worker.disconnect()
        return HTMLResponse(
            f'<span style="color:var(--green)">&#10003; Connected — '
            f'{info["inbox_total"]} msgs, {info["inbox_unread"]} unread</span>')
    else:
        return HTMLResponse('<span style="color:var(--red)">&#10007; Connection failed</span>')


# --- Campaigns ---

@router.post("/warmup/campaigns/add")
async def add_campaign(request: Request,
                       name: str = Form(""),
                       sending_domain: str = Form(""),
                       smtp_preset_id: int = Form(0),
                       template_id: int = Form(0),
                       from_email: str = Form(""),
                       from_name: str = Form(""),
                       curve_type: str = Form("turbo")):
    db = request.app.state.db
    if not name.strip() or not sending_domain.strip():
        return RedirectResponse("/warmup", status_code=303)
    db.create_warmup_campaign(
        name=name.strip(), sending_domain=sending_domain.strip(),
        smtp_preset_id=smtp_preset_id, template_id=template_id,
        from_email=from_email.strip(), from_name=from_name.strip(),
        curve_type=curve_type)
    return RedirectResponse("/warmup", status_code=303)


@router.post("/warmup/campaigns/{cid}/run", response_class=HTMLResponse)
async def run_campaign(request: Request, cid: int):
    db = request.app.state.db
    if cid in _engines:
        return HTMLResponse('<span style="color:var(--fg2)">Already running</span>')

    from bulk.mailer.warmup_engine import WarmupEngine
    engine = WarmupEngine(db)
    _engines[cid] = engine

    def worker():
        try:
            engine.run_campaign(cid)
        except Exception as e:
            logger.error("Warmup campaign %d error: %s", cid, e, exc_info=True)
        finally:
            _engines.pop(cid, None)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return HTMLResponse('<span class="badge badge-running">Started</span>')


@router.post("/warmup/campaigns/{cid}/delete")
async def delete_campaign(request: Request, cid: int):
    if cid in _engines:
        _engines[cid].stop()
        _engines.pop(cid, None)
    request.app.state.db.delete_warmup_campaign(cid)
    return RedirectResponse("/warmup", status_code=303)


@router.post("/warmup/llm/save")
async def save_llm_config(request: Request,
                          api_url: str = Form(""),
                          api_key: str = Form(""),
                          model: str = Form(""),
                          language: str = Form("de")):
    request.app.state.db.save_llm_config(
        api_url.strip() or "https://api.openai.com/v1/chat/completions",
        api_key.strip(), model.strip() or "gpt-4o-mini", language)
    return RedirectResponse("/warmup", status_code=303)


@router.post("/warmup/llm/test", response_class=HTMLResponse)
async def test_llm(request: Request):
    db = request.app.state.db
    cfg = db.get_llm_config()
    if not cfg.get("api_key"):
        return HTMLResponse('<span style="color:var(--red)">No API key configured</span>')
    from bulk.mailer.warmup_ai import generate_reply
    reply = generate_reply(cfg["api_url"], cfg["api_key"], cfg["model"],
                           "Newsletter Update", language=cfg.get("language", "de"))
    if reply:
        from html import escape
        return HTMLResponse(f'<span style="color:var(--green)">&#10003; LLM works: "{escape(reply)}"</span>')
    return HTMLResponse('<span style="color:var(--red)">&#10007; LLM call failed</span>')


@router.get("/warmup/log", response_class=HTMLResponse)
async def warmup_log(request: Request):
    db = request.app.state.db
    actions = [dict(a) for a in db.get_warmup_log(limit=50)]
    if not actions:
        return HTMLResponse('<div class="empty-state"><p>No activity yet.</p></div>')
    rows = ""
    for a in actions:
        result_color = "var(--green)" if a.get("executed_at") and "error" not in (a.get("result") or "") else \
                       "var(--red)" if a.get("executed_at") else "var(--fg2)"
        result_text = a.get("result") or "ok" if a.get("executed_at") else "pending"
        rows += (f'<tr><td style="font-size:11px;color:var(--fg2)">{a["scheduled_at"]}</td>'
                 f'<td style="font-family:monospace;font-size:12px">{a["email"]}</td>'
                 f'<td><span class="badge badge-info">{a["action_type"]}</span></td>'
                 f'<td style="font-size:12px;color:{result_color}">{result_text}</td></tr>')
    return HTMLResponse(
        f'<table><thead><tr><th>Time</th><th>Seed</th><th>Action</th><th>Result</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>')
