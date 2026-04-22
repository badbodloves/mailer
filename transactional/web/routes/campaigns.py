"""Campaigns — CRUD, start/stop, live stats."""
import re
import json
import time
import logging
import threading
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("trans.campaigns")
router = APIRouter()

_runners = {}
_speed = {}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(request: Request):
    db = request.app.state.db
    campaigns = []
    for c in db.get_campaigns():
        cd = dict(c)
        cd["running"] = cd["id"] in _runners
        cd["speed"] = _speed.get(cd["id"], 0)
        total = cd.get("total_leads", 0) or 0
        sent = cd.get("sent", 0) or 0
        failed = cd.get("failed", 0) or 0
        cd["pct"] = int((sent + failed) / total * 100) if total > 0 else 0
        cd["remaining"] = max(0, total - sent - failed)
        campaigns.append(cd)
    templates = [dict(t) for t in db.get_templates()]
    return request.app.state.templates.TemplateResponse(request, "campaigns.html", {
        "active": "campaigns", "campaigns": campaigns, "templates": templates, "db": db,
    })


@router.post("/campaigns/add")
async def add_campaign(request: Request,
                       name: str = Form(""),
                       from_name: str = Form(""),
                       from_email: str = Form(""),
                       subject: str = Form(""),
                       threads: int = Form(40),
                       leads_text: str = Form("")):
    db = request.app.state.db
    cid = db.create_campaign(
        name=name.strip() or f"Campaign {time.strftime('%Y-%m-%d %H:%M')}",
        from_name=from_name.strip(),
        from_email=from_email.strip(),
        subject=subject.strip(),
        threads=threads,
    )
    if leads_text.strip():
        emails = EMAIL_RE.findall(leads_text)
        if emails:
            db.import_leads(cid, emails)
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/save")
async def save_campaign(request: Request, cid: int):
    db = request.app.state.db
    form = await request.form()
    updates = {}
    for field in ["name", "from_name", "from_email", "subject", "test_recipients",
                   "schedule_time", "redirect_target_url"]:
        if field in form:
            updates[field] = form[field].strip() if isinstance(form[field], str) else str(form[field])
    for field in ["threads", "test_interval", "warmup_count", "logo_max_colors",
                   "logo_rotate_every", "redirect_rotate_every", "proxy_rotate_every"]:
        if field in form:
            try:
                updates[field] = int(form[field])
            except (ValueError, TypeError):
                pass
    for field in ["normal_delay", "provider_delay", "warmup_delay", "structure_variation"]:
        if field in form:
            try:
                updates[field] = float(form[field])
            except (ValueError, TypeError):
                pass
    for field in ["antifingerprint_classes", "advanced_antifingerprint", "image_enabled",
                   "image_quantize", "image_downscale", "redirect_enabled", "ignore_ssl_errors"]:
        updates[field] = 1 if field in form else 0
    if "image_mode" in form:
        updates["image_mode"] = form["image_mode"]
    if updates:
        db.update_campaign(cid, **updates)
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/import-leads")
async def import_leads(request: Request, cid: int, leads_text: str = Form("")):
    db = request.app.state.db
    emails = EMAIL_RE.findall(leads_text)
    if emails:
        count = db.import_leads(cid, emails)
        db.update_campaign(cid, total_leads=db.get_lead_count(cid))
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/reset")
async def reset_campaign(request: Request, cid: int):
    if cid in _runners:
        return RedirectResponse("/campaigns", status_code=303)
    request.app.state.db.reset_leads(cid)
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/start")
async def start_campaign(request: Request, cid: int):
    db = request.app.state.db
    if cid in _runners:
        return RedirectResponse("/campaigns", status_code=303)

    camp = db.get_campaign(cid)
    if not camp:
        return RedirectResponse("/campaigns", status_code=303)

    _speed[cid] = 0

    def run():
        try:
            _run_campaign(db, cid)
        except Exception as e:
            logger.error("Campaign %d CRASHED: %s", cid, e, exc_info=True)
            db.update_campaign(cid, status="FAILED")
        finally:
            _runners.pop(cid, None)
            _speed.pop(cid, None)

    t = threading.Thread(target=run, daemon=True)
    _runners[cid] = t
    t.start()
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/stop")
async def stop_campaign(request: Request, cid: int):
    _runners.pop(cid, None)
    request.app.state.db.update_campaign(cid, status="PAUSED")
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/delete")
async def delete_campaign(request: Request, cid: int):
    _runners.pop(cid, None)
    request.app.state.db.delete_campaign(cid)
    return RedirectResponse("/campaigns", status_code=303)


@router.get("/campaigns/{cid}/stats", response_class=HTMLResponse)
async def campaign_stats(request: Request, cid: int):
    db = request.app.state.db
    camp = db.get_campaign(cid)
    if not camp:
        return HTMLResponse("<div>Not found</div>")
    cd = dict(camp)
    total = cd.get("total_leads", 0) or 0
    sent = cd.get("sent", 0) or 0
    failed = cd.get("failed", 0) or 0
    remaining = max(0, total - sent - failed)
    pct = int((sent + failed) / total * 100) if total > 0 else 0
    running = cid in _runners
    speed = _speed.get(cid, 0)
    speed_str = f"{speed:,}/h" if speed else "—"
    status = cd.get("status", "DRAFT")
    return HTMLResponse(f"""
    <div class="grid-4" style="margin-bottom:12px">
        <div class="metric"><div class="value" style="color:var(--green)">{sent:,}</div><div class="label">Sent</div></div>
        <div class="metric"><div class="value" style="color:var(--red)">{failed:,}</div><div class="label">Failed</div></div>
        <div class="metric"><div class="value">{remaining:,}</div><div class="label">Remaining</div></div>
        <div class="metric"><div class="value">{speed_str}</div><div class="label">Speed</div></div>
    </div>
    <div class="progress"><div class="progress-bar" style="width:{pct}%">{pct}%</div></div>
    <p style="margin-top:8px;font-size:12px;color:var(--fg2)">
        Status: <span class="badge badge-{'running' if running else status.lower()}">{status}</span>
        &nbsp; Threads: {cd.get('threads', 40)}
    </p>""")


@router.get("/campaigns/table", response_class=HTMLResponse)
async def campaigns_table(request: Request):
    db = request.app.state.db
    campaigns = []
    for c in db.get_campaigns():
        cd = dict(c)
        cd["running"] = cd["id"] in _runners
        cd["speed"] = _speed.get(cd["id"], 0)
        total = cd.get("total_leads", 0) or 0
        sent = cd.get("sent", 0) or 0
        failed = cd.get("failed", 0) or 0
        cd["pct"] = int((sent + failed) / total * 100) if total > 0 else 0
        cd["remaining"] = max(0, total - sent - failed)
        campaigns.append(cd)

    html = f'<div class="card-header"><h3>Campaigns <span class="count">{len(campaigns)}</span></h3></div>'
    html += '<table><thead><tr><th>Name</th><th>Subject</th><th>Progress</th><th>Total</th><th>Sent</th><th>Failed</th><th>Speed</th><th></th></tr></thead><tbody>'
    for c in campaigns:
        bg = ' style="background:var(--green-light)"' if c["running"] else ""
        speed = f'{c["speed"]:,}/h' if c["speed"] else "—"
        subj = (c.get("subject") or "—")[:40]
        btn = (f'<form method="post" action="/campaigns/{c["id"]}/stop" style="display:inline">'
               f'<button class="btn btn-danger btn-xs">Stop</button></form>') if c["running"] else \
              (f'<form method="post" action="/campaigns/{c["id"]}/start" style="display:inline">'
               f'<button class="btn btn-success btn-xs">Start</button></form>')
        html += (f'<tr{bg}><td style="font-weight:500">{c["name"]}</td>'
                 f'<td style="font-size:12px;color:var(--fg2)">{subj}</td>'
                 f'<td style="min-width:100px"><div class="progress"><div class="progress-bar" style="width:{c["pct"]}%">{c["pct"]}%</div></div></td>'
                 f'<td>{c.get("total_leads",0) or 0:,}</td>'
                 f'<td style="color:var(--green)">{c.get("sent",0) or 0:,}</td>'
                 f'<td style="color:var(--red)">{c.get("failed",0) or 0:,}</td>'
                 f'<td>{speed}</td><td>{btn}</td></tr>')
    html += '</tbody></table>'
    return HTMLResponse(html)


def _run_campaign(db, cid: int):
    """Execute a transactional campaign using the mailer core modules."""
    from mailer.content_engine import ContentEngine
    from mailer.mime_builder import MIMEBuilder
    from mailer.smtp_worker import SMTPPool, SMTPWorker, SendResult
    from mailer.antifingerprint import AntiFingerprintEngine
    from mailer.advanced_antifingerprint import AdvancedAntiFingerprintEngine
    from email.utils import formatdate
    import os
    import tempfile

    camp = dict(db.get_campaign(cid))
    db.update_campaign(cid, status="RUNNING")
    db.reset_in_progress(cid)

    states = db.get_lead_states(cid)
    pending = states.get("PENDING", 0)
    if pending == 0:
        total = sum(states.values())
        if total > 0:
            logger.info("Campaign %d: 0 pending, %d total — resetting", cid, total)
            db.reset_leads(cid)
            pending = total

    logger.info("Campaign %d starting: %d pending leads", cid, pending)

    smtps = db.get_smtps()
    if not smtps:
        logger.error("Campaign %d: no SMTPs configured", cid)
        db.update_campaign(cid, status="FAILED")
        return

    smtp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    for s in smtps:
        s = dict(s)
        if s.get("is_dead"):
            continue
        line = f"{s['host']},{s['port']},{s['username']},{s['password']}"
        if s.get("proxy"):
            line += f",{s['proxy']}"
        smtp_file.write(line + "\n")
    smtp_file.close()

    try:
        pool = SMTPPool(
            smtp_file.name,
            timeout=30,
            warmup_delay=camp.get("warmup_delay", 30.0),
            warmup_count=camp.get("warmup_count", 5),
            ignore_ssl_errors=bool(camp.get("ignore_ssl_errors", 1)),
        )
        worker = SMTPWorker(
            pool,
            normal_delay=camp.get("normal_delay", 0.3),
            provider_delay=camp.get("provider_delay", 6.0),
        )

        if camp.get("advanced_antifingerprint"):
            afp = AdvancedAntiFingerprintEngine(
                enable_classes=bool(camp.get("antifingerprint_classes", 1)),
                structure_variation=camp.get("structure_variation", 0.5),
            )
        elif camp.get("antifingerprint_classes"):
            afp = AntiFingerprintEngine(enable_classes=True)
        else:
            afp = None

        templates = [dict(t) for t in db.get_templates()]
        names_pool = db.get_pool("names")
        subjects_pool = db.get_pool("subjects")
        names_list = [n.strip() for n in names_pool.splitlines() if n.strip()] if names_pool else []
        subjects_list = [s.strip() for s in subjects_pool.splitlines() if s.strip()] if subjects_pool else []

        from_email = camp.get("from_email", "")
        from_name = camp.get("from_name", "") or "Newsletter"
        subject_tpl = camp.get("subject", "") or "Notification"

        import random

        sent = 0
        failed = 0
        last_check = time.monotonic()
        last_sent = 0

        def speed_tracker():
            nonlocal last_check, last_sent
            while cid in _runners:
                time.sleep(5)
                try:
                    now_t = time.monotonic()
                    elapsed = now_t - last_check
                    if elapsed > 0:
                        rate = (sent - last_sent) / elapsed * 3600
                        _speed[cid] = int(rate)
                    last_check = now_t
                    last_sent = sent
                except Exception:
                    pass

        st = threading.Thread(target=speed_tracker, daemon=True)
        st.start()

        while cid in _runners:
            batch = db.fetch_pending(cid, 200)
            if not batch:
                break

            lead_ids = [r["id"] for r in batch]
            db.mark_in_progress(lead_ids)

            for row in batch:
                if cid not in _runners:
                    break

                lead_id = row["id"]
                email = row["email"]

                account = pool.acquire()
                if account is None:
                    if pool.all_dead:
                        logger.error("Campaign %d: all SMTPs dead", cid)
                        _runners.pop(cid, None)
                        break
                    time.sleep(5)
                    continue

                cur_from_email = from_email or account.user
                cur_from_name = random.choice(names_list) if names_list else from_name
                cur_subject = random.choice(subjects_list) if subjects_list else subject_tpl

                html_body = "<p>Hello {email_user},</p><p>This is your notification.</p>"
                if templates:
                    tpl = random.choice(templates)
                    if tpl.get("html_content"):
                        html_body = tpl["html_content"]

                html_body = html_body.replace("{email}", email)
                html_body = html_body.replace("{email_user}", email.split("@")[0])
                html_body = html_body.replace("{domain}", email.split("@")[1] if "@" in email else "")
                cur_from_name = cur_from_name.replace("{email_user}", email.split("@")[0])
                cur_subject = cur_subject.replace("{email_user}", email.split("@")[0])
                cur_subject = cur_subject.replace("{email}", email)

                if afp:
                    html_body = afp.transform(html_body)

                plain_body = html_body.replace("<br>", "\n").replace("<br/>", "\n")
                plain_body = re.sub(r"<[^>]+>", "", plain_body).strip()

                raw_msg = MIMEBuilder.build_email(
                    from_name=cur_from_name,
                    from_email=cur_from_email,
                    to_email=email,
                    subject=cur_subject,
                    html_body=html_body,
                    plain_body=plain_body,
                )

                result = worker.send(cur_from_email, email, raw_msg, account=account)

                if result.is_success:
                    db.mark_sent(lead_id)
                    sent += 1
                    delay = worker.get_delay(email)
                    if delay > 0:
                        time.sleep(delay)
                elif result.is_fatal:
                    db.mark_failed(lead_id, result.error[:500])
                    failed += 1
                else:
                    db._conn().execute("UPDATE trans_leads SET state='PENDING' WHERE id=?", (lead_id,))
                    db._conn().commit()

                db.update_campaign(cid, sent=sent, failed=failed)

        status = "FINISHED" if cid not in _runners else "PAUSED"
        logger.info("Campaign %d %s: sent=%d, failed=%d", cid, status, sent, failed)
        db.update_campaign(cid, status=status, sent=sent, failed=failed)

    finally:
        os.unlink(smtp_file.name)
