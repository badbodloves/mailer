"""Campaigns — CRUD, start/stop, live stats, test-send, pause, events."""
import re
import json
import time
import logging
import threading
import smtplib
import ssl
from html import escape
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


@router.post("/campaigns/{cid}/test-send", response_class=HTMLResponse)
async def test_send(request: Request, cid: int):
    db = request.app.state.db
    camp = db.get_campaign(cid)
    if not camp:
        return HTMLResponse('<div class="alert alert-danger">Campaign not found</div>')
    camp = dict(camp)
    test_recips = camp.get("test_recipients", "")
    emails = EMAIL_RE.findall(test_recips)
    if not emails:
        return HTMLResponse('<div class="alert alert-warning">No test recipients configured</div>')

    smtps = db.get_smtps()
    if not smtps:
        return HTMLResponse('<div class="alert alert-danger">No SMTPs configured</div>')

    smtp_row = None
    for s in smtps:
        s = dict(s)
        if not s.get("is_dead"):
            smtp_row = s
            break
    if not smtp_row:
        return HTMLResponse('<div class="alert alert-danger">All SMTPs are dead</div>')

    templates = [dict(t) for t in db.get_templates()]
    html_bodies = [t["html_content"] for t in templates if t.get("html_content")]
    if not html_bodies:
        return HTMLResponse('<div class="alert alert-warning">No templates available</div>')

    results = []
    for to_email in emails:
        try:
            html_body = html_bodies[0]
            html_body = html_body.replace("{email}", to_email)
            html_body = html_body.replace("{email_user}", to_email.split("@")[0])
            html_body = html_body.replace("{domain}", to_email.split("@")[1] if "@" in to_email else "")

            from mailer.mime_builder import MIMEBuilder
            plain_body = re.sub(r"<br\s*/?>", "\n", html_body)
            plain_body = re.sub(r"<[^>]+>", "", plain_body).strip()
            raw_msg = MIMEBuilder.build_email(
                from_name=camp.get("from_name", "") or "Test",
                from_email=camp.get("from_email", "") or smtp_row["username"],
                to_email=to_email,
                subject=camp.get("subject", "") or "Test Email",
                html_body=html_body,
                plain_body=plain_body,
            )

            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            port = smtp_row["port"]
            if port == 465:
                server = smtplib.SMTP_SSL(smtp_row["host"], port, timeout=15, context=ctx)
            else:
                server = smtplib.SMTP(smtp_row["host"], port, timeout=15)
                server.ehlo()
                if server.has_extn("starttls"):
                    server.starttls(context=ctx)
                    server.ehlo()
            server.login(smtp_row["username"], smtp_row["password"])
            from_addr = camp.get("from_email", "") or smtp_row["username"]
            server.sendmail(from_addr, to_email, raw_msg)
            server.quit()
            results.append(f'<span style="color:var(--green)">&#10003; {escape(to_email)}</span>')
        except Exception as e:
            results.append(f'<span style="color:var(--red)">&#10007; {escape(to_email)}: {escape(str(e)[:200])}</span>')

    return HTMLResponse('<div class="alert alert-info">' + '<br>'.join(results) + '</div>')


@router.post("/campaigns/{cid}/pause", response_class=HTMLResponse)
async def pause_campaign(request: Request, cid: int):
    db = request.app.state.db
    camp = db.get_campaign(cid)
    if not camp:
        return HTMLResponse('<div class="alert alert-danger">Campaign not found</div>')
    _runners.pop(cid, None)
    _speed.pop(cid, None)
    db.update_campaign(cid, status="PAUSED")
    return HTMLResponse('<div class="alert alert-success">Campaign paused</div>')


@router.get("/campaigns/{cid}/events", response_class=HTMLResponse)
async def campaign_events(request: Request, cid: int):
    db = request.app.state.db
    camp = db.get_campaign(cid)
    if not camp:
        return HTMLResponse('<div class="alert alert-danger">Campaign not found</div>')

    rows = db._conn().execute(
        "SELECT id, email, state, error_msg FROM trans_leads "
        "WHERE campaign_id=? AND state IN ('SENT','FAILED') "
        "ORDER BY id DESC LIMIT 100", (cid,)
    ).fetchall()

    if not rows:
        return HTMLResponse('<p style="color:var(--fg2)">No events yet</p>')

    html = '<table><thead><tr><th>Email</th><th>Status</th><th>Error</th></tr></thead><tbody>'
    for r in rows:
        r = dict(r)
        state = r["state"]
        badge_cls = "running" if state == "SENT" else "failed"
        err = escape(r.get("error_msg", "") or "")[:120]
        html += (f'<tr>'
                 f'<td style="font-family:monospace;font-size:12px">{escape(r["email"])}</td>'
                 f'<td><span class="badge badge-{badge_cls}">{escape(state)}</span></td>'
                 f'<td style="font-size:11px;color:var(--red)">{err}</td>'
                 f'</tr>')
    html += '</tbody></table>'
    return HTMLResponse(html)


def _run_campaign(db, cid: int):
    """Execute a transactional campaign with multi-threading and full content engine."""
    from mailer.content_engine import ContentEngine
    from mailer.mime_builder import MIMEBuilder
    from mailer.smtp_worker import SMTPPool, SMTPWorker, SendResult
    from mailer.antifingerprint import AntiFingerprintEngine
    from mailer.advanced_antifingerprint import AdvancedAntiFingerprintEngine
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import os
    import random
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
            smtp_file.name, timeout=30,
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
        html_bodies = [t["html_content"] for t in templates if t.get("html_content")]

        names_pool = db.get_pool("names")
        subjects_pool = db.get_pool("subjects")
        spintax_pools = {}
        for p in db.get_pools("spintax"):
            pd = dict(p)
            if pd.get("content"):
                lines = [l.strip() for l in pd["content"].splitlines() if l.strip()]
                if lines:
                    spintax_pools[pd["name"]] = lines

        names_list = [n.strip() for n in names_pool.splitlines() if n.strip()] if names_pool else []
        subjects_list = [s.strip() for s in subjects_pool.splitlines() if s.strip()] if subjects_pool else []

        from_email_cfg = camp.get("from_email", "")
        from_name_cfg = camp.get("from_name", "") or "Newsletter"
        subject_cfg = camp.get("subject", "") or "Notification"

        thread_count = min(camp.get("threads", 40) or 40, pool.size * 2, 200)
        thread_count = max(thread_count, 1)
        logger.info("Campaign %d starting: %d pending, %d SMTPs, %d threads, %d templates",
                     cid, pending, pool.size, thread_count, len(html_bodies))

        sent = 0
        failed = 0
        _lock = threading.Lock()

        def speed_tracker():
            nonlocal sent
            last_s, last_t = 0, time.monotonic()
            while cid in _runners:
                time.sleep(5)
                try:
                    now_t = time.monotonic()
                    elapsed = now_t - last_t
                    if elapsed > 0:
                        _speed[cid] = int((sent - last_s) / elapsed * 3600)
                    last_t, last_s = now_t, sent
                except Exception:
                    pass

        st = threading.Thread(target=speed_tracker, daemon=True)
        st.start()

        def _process_variable(text, email):
            """Resolve all variables and spintax in text."""
            user = email.split("@")[0] if "@" in email else email
            domain = email.split("@")[1] if "@" in email else ""
            text = text.replace("{email}", email)
            text = text.replace("{email_user}", user)
            text = text.replace("{domain}", domain)
            for pool_name, pool_lines in spintax_pools.items():
                text = text.replace(f"{{{pool_name}}}", random.choice(pool_lines))
            import re as _re
            def _resolve_spintax(m):
                opts = m.group(1).split("|")
                return random.choice(opts) if opts else ""
            for _ in range(20):
                new = _re.sub(r"\{([^{}]+\|[^{}]+)\}", _resolve_spintax, text)
                if new == text:
                    break
                text = new
            def _resolve_randstr(m):
                import string
                length = int(m.group(1))
                charset_name = m.group(2)
                case = m.group(3)
                charsets = {
                    "a-z": string.ascii_lowercase, "A-Z": string.ascii_uppercase,
                    "0-9": string.digits, "a-z0-9": string.ascii_lowercase + string.digits,
                    "A-Z0-9": string.ascii_uppercase + string.digits,
                    "a-zA-Z": string.ascii_letters,
                    "a-zA-Z0-9": string.ascii_letters + string.digits,
                }
                chars = charsets.get(charset_name, string.ascii_lowercase)
                result = "".join(random.choice(chars) for _ in range(length))
                if case == "upper":
                    result = result.upper()
                elif case == "lower":
                    result = result.lower()
                return result
            text = _re.sub(r"\[RANDSTR:(\d+):([a-zA-Z0-9\-]+):(\w+)\]", _resolve_randstr, text)
            return text

        def _send_one(lead_id, email):
            nonlocal sent, failed
            if cid not in _runners:
                return

            account = pool.acquire()
            if account is None:
                if pool.all_dead:
                    return
                time.sleep(3)
                account = pool.acquire()
                if account is None:
                    return

            cur_from_email = from_email_cfg or account.user
            cur_from_name = _process_variable(
                random.choice(names_list) if names_list else from_name_cfg, email)
            cur_subject = _process_variable(
                random.choice(subjects_list) if subjects_list else subject_cfg, email)

            html_body = random.choice(html_bodies) if html_bodies else \
                "<p>Hello {email_user},</p><p>This is your notification.</p>"
            html_body = _process_variable(html_body, email)

            if afp:
                html_body = afp.transform(html_body)

            plain_body = re.sub(r"<br\s*/?>", "\n", html_body)
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

            with _lock:
                if result.is_success:
                    db.mark_sent(lead_id)
                    sent += 1
                elif result.is_fatal:
                    db.mark_failed(lead_id, result.error[:500])
                    failed += 1
                else:
                    db._conn().execute(
                        "UPDATE trans_leads SET state='PENDING' WHERE id=?", (lead_id,))
                    db._conn().commit()
                db.update_campaign(cid, sent=sent, failed=failed)

            if result.is_success:
                delay = worker.get_delay(email)
                if delay > 0:
                    time.sleep(delay)

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            while cid in _runners:
                if pool.all_dead:
                    logger.error("Campaign %d: all SMTPs dead", cid)
                    break

                batch = db.fetch_pending(cid, 200)
                if not batch:
                    break

                lead_ids = [r["id"] for r in batch]
                db.mark_in_progress(lead_ids)

                futures = {}
                for row in batch:
                    if cid not in _runners:
                        break
                    f = executor.submit(_send_one, row["id"], row["email"])
                    futures[f] = row["id"]

                for f in as_completed(futures):
                    try:
                        f.result(timeout=120)
                    except Exception as e:
                        lid = futures[f]
                        with _lock:
                            db.mark_failed(lid, str(e)[:500])
                            failed += 1

        status = "FINISHED" if cid not in _runners else "PAUSED"
        logger.info("Campaign %d %s: sent=%d, failed=%d", cid, status, sent, failed)
        db.update_campaign(cid, status=status, sent=sent, failed=failed)
        db.reset_in_progress(cid)

    finally:
        os.unlink(smtp_file.name)
