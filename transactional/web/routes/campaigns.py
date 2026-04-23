"""Campaigns — CRUD, start/stop, live stats."""
import re
import time
import random
import logging
import threading
from html import escape
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("trans.campaigns")
router = APIRouter()

_runners = {}
_speed = {}


def _enrich(db, cd):
    cd["running"] = cd["id"] in _runners
    cd["speed"] = _speed.get(cd["id"], 0)
    total = cd.get("total_leads", 0) or 0
    sent = cd.get("sent", 0) or 0
    failed = cd.get("failed", 0) or 0
    cd["pct"] = int((sent + failed) / total * 100) if total > 0 else 0
    cd["remaining"] = max(0, total - sent - failed)
    try:
        sl = db._conn().execute("SELECT name FROM trans_smtp_lists WHERE id=?",
                                 (cd.get("smtp_list_id", 0),)).fetchone()
        cd["smtp_list_name"] = sl["name"] if sl else "—"
    except Exception:
        cd["smtp_list_name"] = "—"
    try:
        ll = db._conn().execute("SELECT name FROM trans_lead_lists WHERE id=?",
                                 (cd.get("lead_list_id", 0),)).fetchone()
        cd["lead_list_name"] = ll["name"] if ll else "—"
    except Exception:
        cd["lead_list_name"] = "—"
    try:
        cd["smtp_list_count"] = db.get_smtp_count(cd.get("smtp_list_id", 0))
    except Exception:
        cd["smtp_list_count"] = 0
    try:
        cd["lead_list_count"] = db.get_lead_count(cd.get("lead_list_id", 0))
    except Exception:
        cd["lead_list_count"] = 0
    return cd


@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(request: Request):
    db = request.app.state.db
    campaigns = [_enrich(db, dict(c)) for c in db.get_campaigns()]
    smtp_lists = [dict(sl, count=db.get_smtp_count(sl["id"])) for sl in db.get_smtp_lists()]
    lead_lists = [dict(ll, count=db.get_lead_count(ll["id"])) for ll in db.get_lead_lists()]
    return request.app.state.templates.TemplateResponse(request, "campaigns.html", {
        "active": "campaigns", "campaigns": campaigns,
        "smtp_lists": smtp_lists, "lead_lists": lead_lists, "db": db,
    })


@router.post("/campaigns/add")
async def add_campaign(request: Request,
                       name: str = Form(""),
                       smtp_list_id: int = Form(0),
                       lead_list_id: int = Form(0)):
    db = request.app.state.db
    total = db.get_lead_count(lead_list_id) if lead_list_id else 0
    db.create_campaign(
        name=name.strip() or f"Campaign {time.strftime('%Y-%m-%d %H:%M')}",
        smtp_list_id=smtp_list_id, lead_list_id=lead_list_id,
        total_leads=total)
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/save")
async def save_campaign(request: Request, cid: int,
                        name: str = Form(""),
                        smtp_list_id: int = Form(0),
                        lead_list_id: int = Form(0)):
    db = request.app.state.db
    total = db.get_lead_count(lead_list_id) if lead_list_id else 0
    db.update_campaign(cid, name=name.strip(), smtp_list_id=smtp_list_id,
                       lead_list_id=lead_list_id, total_leads=total)
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/reset")
async def reset_campaign(request: Request, cid: int):
    if cid in _runners:
        return RedirectResponse("/campaigns", status_code=303)
    db = request.app.state.db
    camp = db.get_campaign(cid)
    if camp:
        db.reset_leads(camp["lead_list_id"])
        db.update_campaign(cid, sent=0, failed=0, status="DRAFT")
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/start")
async def start_campaign(request: Request, cid: int):
    db = request.app.state.db
    if cid in _runners:
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


@router.post("/campaigns/{cid}/test-send", response_class=HTMLResponse)
async def test_send(request: Request, cid: int):
    """Send test email to test_recipients from config."""
    db = request.app.state.db
    cfg = db.get_config()
    recipients_raw = cfg.get("test_recipients", "")
    if not recipients_raw.strip():
        return HTMLResponse('<div class="alert alert-warning">No test recipients in Config.</div>')

    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    camp = db.get_campaign(cid)
    if not camp:
        return HTMLResponse('<div class="alert alert-danger">Campaign not found.</div>')
    camp = dict(camp)

    smtp_list_id = camp.get("smtp_list_id", 0)
    smtps = [dict(s) for s in db.get_smtps(smtp_list_id)] if smtp_list_id else []
    if not smtps:
        return HTMLResponse('<div class="alert alert-danger">No SMTPs in selected list.</div>')

    results = []
    import smtplib, ssl
    from email.mime.text import MIMEText

    templates = [dict(t) for t in db.get_templates()]
    html_bodies = [t["html_content"] for t in templates if t.get("html_content")]
    html = html_bodies[0] if html_bodies else "<p>Hello {email_user}, this is a test.</p>"

    from_name = cfg.get("from_name", "") or "Test"
    from_email_cfg = cfg.get("from_email", "")
    subject = cfg.get("subject", "") or "Test Email"

    s = smtps[0]
    from_email = from_email_cfg or s["username"]

    for recipient in recipients:
        user = recipient.split("@")[0]
        cur_html = html.replace("{email}", recipient).replace("{email_user}", user).replace("{domain}", recipient.split("@")[1] if "@" in recipient else "")
        cur_subject = subject.replace("{email_user}", user).replace("{email}", recipient)
        cur_from = from_name.replace("{email_user}", user)

        plain = re.sub(r"<[^>]+>", "", cur_html).strip()

        try:
            from mailer.mime_builder import MIMEBuilder
            raw_msg = MIMEBuilder.build_email(
                from_name=cur_from, from_email=from_email,
                to_email=recipient, subject=f"[TEST] {cur_subject}",
                html_body=cur_html, plain_body=plain)

            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            if s["port"] == 465:
                server = smtplib.SMTP_SSL(s["host"], s["port"], timeout=15, context=ctx)
            else:
                server = smtplib.SMTP(s["host"], s["port"], timeout=15)
                server.ehlo()
                if server.has_extn("starttls"):
                    server.starttls(context=ctx)
                    server.ehlo()
            server.login(s["username"], s["password"])
            server.sendmail(from_email, [recipient], raw_msg)
            server.quit()
            results.append(f'<div style="color:var(--green);font-size:13px">&#10003; Sent to {escape(recipient)}</div>')
        except Exception as e:
            results.append(f'<div style="color:var(--red);font-size:13px">&#10007; {escape(recipient)}: {escape(str(e)[:100])}</div>')

    return HTMLResponse("".join(results))


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
    </p>""")


@router.get("/campaigns/table", response_class=HTMLResponse)
async def campaigns_table(request: Request):
    db = request.app.state.db
    campaigns = [_enrich(db, dict(c)) for c in db.get_campaigns()]
    html = f'<div class="card-header"><h3>Campaigns <span class="count">{len(campaigns)}</span></h3></div>'
    html += '<table><thead><tr><th>Name</th><th>SMTP</th><th>Leads</th><th>Progress</th><th>Total</th><th>Sent</th><th>Failed</th><th>Speed</th><th></th></tr></thead><tbody>'
    for c in campaigns:
        bg = ' style="background:var(--green-light)"' if c["running"] else ""
        speed = f'{c["speed"]:,}/h' if c["speed"] else "—"
        btn = (f'<form method="post" action="/campaigns/{c["id"]}/stop" style="display:inline">'
               f'<button class="btn btn-danger btn-xs">Stop</button></form>') if c["running"] else \
              (f'<form method="post" action="/campaigns/{c["id"]}/start" style="display:inline">'
               f'<button class="btn btn-success btn-xs">Start</button></form>')
        html += (f'<tr{bg}><td style="font-weight:500">{c["name"]}</td>'
                 f'<td style="font-size:12px">{c["smtp_list_name"]}</td>'
                 f'<td style="font-size:12px">{c["lead_list_name"]}</td>'
                 f'<td style="min-width:100px"><div class="progress"><div class="progress-bar" style="width:{c["pct"]}%">{c["pct"]}%</div></div></td>'
                 f'<td>{c.get("total_leads",0) or 0:,}</td>'
                 f'<td style="color:var(--green)">{c.get("sent",0) or 0:,}</td>'
                 f'<td style="color:var(--red)">{c.get("failed",0) or 0:,}</td>'
                 f'<td>{speed}</td><td>{btn}</td></tr>')
    html += '</tbody></table>'
    return HTMLResponse(html)


def _run_campaign(db, cid: int):
    """Execute campaign using mailer core modules + DB config."""
    from mailer.mime_builder import MIMEBuilder
    from mailer.smtp_worker import SMTPPool, SMTPWorker, SendResult
    from mailer.antifingerprint import AntiFingerprintEngine
    from mailer.advanced_antifingerprint import AdvancedAntiFingerprintEngine
    import os, tempfile

    camp = dict(db.get_campaign(cid))
    cfg = db.get_config()
    db.update_campaign(cid, status="RUNNING")

    lead_list_id = camp.get("lead_list_id", 0)
    smtp_list_id = camp.get("smtp_list_id", 0)

    if not lead_list_id or not smtp_list_id:
        logger.error("Campaign %d: no SMTP or lead list", cid)
        db.update_campaign(cid, status="FAILED")
        return

    db.reset_in_progress(lead_list_id)
    states = db.get_lead_states(lead_list_id)
    pending = states.get("PENDING", 0)
    if pending == 0:
        total = sum(states.values())
        if total > 0:
            db.reset_leads(lead_list_id)
            pending = total

    smtps = [dict(s) for s in db.get_smtps(smtp_list_id)]
    if not smtps:
        db.update_campaign(cid, status="FAILED")
        return

    # Write temp SMTP file for SMTPPool
    smtp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    for s in smtps:
        if s.get("is_dead"):
            continue
        smtp_file.write(f"{s['host']},{s['port']},{s['username']},{s['password']}\n")
    smtp_file.close()

    # Write temp proxy file if configured
    proxy_file_path = ""
    proxy_mode = cfg.get("proxy_mode", "off")
    proxy_value = cfg.get("proxy_value", "")
    if proxy_mode != "off" and proxy_value.strip():
        proxy_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        for line in proxy_value.strip().splitlines():
            if line.strip():
                proxy_file.write(line.strip() + "\n")
        proxy_file.close()
        proxy_file_path = proxy_file.name

    try:
        pool = SMTPPool(
            smtp_file.name, timeout=cfg.get("smtp_timeout", 30),
            warmup_delay=cfg.get("warmup_delay", 30.0),
            warmup_count=cfg.get("warmup_count", 5),
            ignore_ssl_errors=cfg.get("ignore_ssl_errors", True),
            proxy_file=proxy_file_path,
            proxy_rotate_every=cfg.get("proxy_rotate_every", 0),
        )
        worker = SMTPWorker(pool,
            normal_delay=cfg.get("normal_delay", 0.3),
            provider_delay=cfg.get("provider_delay", 6.0))

        if cfg.get("advanced_antifingerprint"):
            afp = AdvancedAntiFingerprintEngine(
                enable_classes=cfg.get("antifingerprint_classes", True),
                structure_variation=cfg.get("structure_variation", 0.5))
        elif cfg.get("antifingerprint_classes"):
            afp = AntiFingerprintEngine(enable_classes=True)
        else:
            afp = None

        templates = [dict(t) for t in db.get_templates()]
        html_bodies = [t["html_content"] for t in templates if t.get("html_content")]

        macros = {}
        for m in db.get_macros():
            md = dict(m)
            lines = [l.strip() for l in (md.get("values_text") or "").splitlines() if l.strip()]
            if lines:
                macros[md["name"]] = lines

        from_name_cfg = cfg.get("from_name", "") or "Newsletter"
        from_email_cfg = cfg.get("from_email", "")
        subject_cfg = cfg.get("subject", "") or "Notification"

        thread_count = min(cfg.get("threads", 40), pool.size * 2, 200)
        thread_count = max(thread_count, 1)
        logger.info("Campaign %d starting: %d pending, %d SMTPs, %d threads",
                     cid, pending, pool.size, thread_count)

        sent = 0
        failed = 0
        _lock = threading.Lock()

        test_interval = cfg.get("test_interval", 0)
        interval_recips_raw = cfg.get("interval_recipients", "") or cfg.get("test_recipients", "")
        interval_recips = [r.strip() for r in interval_recips_raw.split(",") if r.strip()]

        def speed_tracker():
            nonlocal sent
            last_s, last_t = 0, time.monotonic()
            while cid in _runners:
                time.sleep(5)
                now_t = time.monotonic()
                elapsed = now_t - last_t
                if elapsed > 0:
                    _speed[cid] = int((sent - last_s) / elapsed * 3600)
                last_t, last_s = now_t, sent

        threading.Thread(target=speed_tracker, daemon=True).start()

        def _process(text, email):
            user = email.split("@")[0] if "@" in email else email
            domain = email.split("@")[1] if "@" in email else ""
            text = text.replace("{email}", email).replace("{email_user}", user).replace("{domain}", domain)
            for mname, mlines in macros.items():
                text = text.replace(f"{{{mname}}}", random.choice(mlines))
            def _spintax(m):
                return random.choice(m.group(1).split("|"))
            for _ in range(20):
                new = re.sub(r"\{([^{}]+\|[^{}]+)\}", _spintax, text)
                if new == text:
                    break
                text = new
            import string
            def _randstr(m):
                length, charset_name, case = int(m.group(1)), m.group(2), m.group(3)
                cs = {"a-z": string.ascii_lowercase, "A-Z": string.ascii_uppercase,
                      "0-9": string.digits, "a-z0-9": string.ascii_lowercase + string.digits,
                      "a-zA-Z0-9": string.ascii_letters + string.digits}.get(charset_name, string.ascii_lowercase)
                r = "".join(random.choice(cs) for _ in range(length))
                return r.upper() if case == "upper" else r.lower() if case == "lower" else r
            text = re.sub(r"\[RANDSTR:(\d+):([a-zA-Z0-9\-]+):(\w+)\]", _randstr, text)
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
            cur_from_name = _process(from_name_cfg, email)
            cur_subject = _process(subject_cfg, email)

            html = random.choice(html_bodies) if html_bodies else "<p>Hello {email_user}</p>"
            html = _process(html, email)
            if afp:
                html = afp.transform(html)
            plain = re.sub(r"<br\s*/?>", "\n", html)
            plain = re.sub(r"<[^>]+>", "", plain).strip()

            raw_msg = MIMEBuilder.build_email(
                from_name=cur_from_name, from_email=cur_from_email,
                to_email=email, subject=cur_subject,
                html_body=html, plain_body=plain)

            result = worker.send(cur_from_email, email, raw_msg, account=account)

            with _lock:
                if result.is_success:
                    db.mark_sent(lead_id)
                    sent += 1
                elif result.is_fatal:
                    db.mark_failed(lead_id, result.error[:500])
                    failed += 1
                else:
                    db._conn().execute("UPDATE trans_leads SET state='PENDING' WHERE id=?", (lead_id,))
                    db._conn().commit()
                db.update_campaign(cid, sent=sent, failed=failed)

            if result.is_success:
                if test_interval > 0 and interval_recips and sent % test_interval == 0:
                    for tr in interval_recips:
                        try:
                            t_account = pool.acquire()
                            if not t_account:
                                continue
                            t_from = from_email_cfg or t_account.user
                            t_html = _process(html_bodies[0] if html_bodies else "<p>Interval test #{sent}</p>", tr)
                            t_plain = re.sub(r"<[^>]+>", "", t_html).strip()
                            t_msg = MIMEBuilder.build_email(
                                from_name=_process(from_name_cfg, tr), from_email=t_from,
                                to_email=tr, subject=f"[TEST #{sent}] {_process(subject_cfg, tr)}",
                                html_body=t_html, plain_body=t_plain)
                            worker.send(t_from, tr, t_msg, account=t_account)
                            logger.info("Interval test #%d sent to %s", sent, tr)
                        except Exception as te:
                            logger.warning("Interval test failed: %s", te)

                delay = worker.get_delay(email)
                if delay > 0:
                    time.sleep(delay)

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            while cid in _runners:
                if pool.all_dead:
                    break
                batch = db.fetch_pending(lead_list_id, 200)
                if not batch:
                    break
                db.mark_in_progress([r["id"] for r in batch])
                futures = {executor.submit(_send_one, r["id"], r["email"]): r["id"] for r in batch}
                for f in as_completed(futures):
                    try:
                        f.result(timeout=120)
                    except Exception as e:
                        with _lock:
                            db.mark_failed(futures[f], str(e)[:500])
                            failed += 1

        status = "FINISHED" if cid not in _runners else "PAUSED"
        logger.info("Campaign %d %s: sent=%d, failed=%d", cid, status, sent, failed)
        db.update_campaign(cid, status=status, sent=sent, failed=failed)
        db.reset_in_progress(lead_list_id)

    finally:
        os.unlink(smtp_file.name)
        if proxy_file_path:
            os.unlink(proxy_file_path)
