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
    if not smtp_list_id or not lead_list_id:
        return RedirectResponse("/campaigns", status_code=303)
    total = db.get_lead_count(lead_list_id)
    db.create_campaign(
        name=name.strip() or f"Campaign {time.strftime('%Y-%m-%d %H:%M')}",
        smtp_list_id=smtp_list_id, lead_list_id=lead_list_id,
        total_leads=total)
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/save")
async def save_campaign(request: Request, cid: int,
                        name: str = Form(""),
                        smtp_list_id: int = Form(0),
                        lead_list_id: int = Form(0),
                        schedule_time: str = Form("")):
    db = request.app.state.db
    updates = {"name": name.strip(), "schedule_time": schedule_time.strip()}
    if smtp_list_id:
        updates["smtp_list_id"] = smtp_list_id
    if lead_list_id:
        updates["lead_list_id"] = lead_list_id
        updates["total_leads"] = db.get_lead_count(lead_list_id)
    db.update_campaign(cid, **updates)
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


@router.post("/campaigns/{cid}/preflight", response_class=HTMLResponse)
async def preflight_check(request: Request, cid: int):
    """Run all pre-flight checks and show Start button only if passed."""
    db = request.app.state.db
    camp = db.get_campaign(cid)
    if not camp:
        return HTMLResponse('<div class="alert alert-danger">Campaign not found.</div>')
    camp = dict(camp)
    cfg = db.get_config()
    log = []
    all_ok = True

    # 1. Check logos
    if cfg.get("image_enabled"):
        import glob, os
        from .logos import VARIANT_DIR, UPLOAD_DIR
        variants = glob.glob(os.path.join(VARIANT_DIR, "*"))
        sources = glob.glob(os.path.join(UPLOAD_DIR, "*"))
        if variants:
            rotate = cfg.get("logo_rotate_every", 0)
            log.append(f'<span style="color:var(--green)">&#10003; {len(variants)} logo variants ready (rotate every {rotate or "send"})</span>')
        elif sources:
            log.append(f'<span style="color:var(--yellow)">&#9888; {len(sources)} source logos but no variants generated. <a href="/logos" style="color:var(--accent)">Generate now</a></span>')
        else:
            log.append('<span style="color:var(--red)">&#10007; No logos uploaded. <a href="/logos" style="color:var(--accent)">Upload</a></span>')
            all_ok = False
    else:
        log.append('<span style="color:var(--fg2)">Logo embedding disabled</span>')

    # 2. Check redirects
    if cfg.get("redirect_enabled"):
        count = db.get_redirect_count()
        if count > 0:
            log.append(f'<span style="color:var(--green)">&#10003; {count} redirect links in pool</span>')
        else:
            log.append('<span style="color:var(--red)">&#10007; No redirect links. <a href="/redirects" style="color:var(--accent)">Generate</a></span>')
            all_ok = False
    else:
        log.append('<span style="color:var(--fg2)">Redirect links disabled</span>')

    # 3. Check templates
    html_count = len(db.get_all_template_htmls())
    if html_count > 0:
        log.append(f'<span style="color:var(--green)">&#10003; {html_count} HTML templates loaded</span>')
    else:
        log.append('<span style="color:var(--red)">&#10007; No HTML templates. <a href="/templates" style="color:var(--accent)">Add</a></span>')
        all_ok = False

    # 4. Send test mail (with retry)
    test_recips = cfg.get("test_recipients", "")
    if test_recips.strip():
        recip = test_recips.split(",")[0].strip()
        smtp_list_id = camp.get("smtp_list_id", 0)
        smtps = [dict(s) for s in db.get_smtps(smtp_list_id)] if smtp_list_id else []
        if smtps:
            import smtplib as _sl, ssl as _ssl
            from transactional.web.routes.smtps import _connect_smtp
            proxy_str = (cfg.get("proxy_value", "").splitlines()[0].strip()
                         if cfg.get("proxy_value", "").strip() else "")
            s = smtps[0]
            test_ok = False
            for attempt in range(3):
                server, error, _ = _connect_smtp(s["host"], s["port"], s["username"], s["password"], proxy_str)
                if server:
                    try:
                        from email.mime.text import MIMEText
                        msg = MIMEText("Pre-flight test", "plain", "utf-8")
                        msg["From"] = s["username"]
                        msg["To"] = recip
                        msg["Subject"] = "[PRE-FLIGHT] Test"
                        server.send_message(msg)
                        server.quit()
                        test_ok = True
                        break
                    except Exception:
                        pass
                import time; time.sleep(2)
            if test_ok:
                log.append(f'<span style="color:var(--green)">&#10003; Test mail sent to {escape(recip)}</span>')
            else:
                log.append(f'<span style="color:var(--red)">&#10007; Test mail failed after 3 attempts</span>')
                all_ok = False
        else:
            log.append('<span style="color:var(--red)">&#10007; No SMTPs in campaign list</span>')
            all_ok = False
    else:
        log.append('<span style="color:var(--fg2)">No test recipients configured</span>')

    html = '<div style="font-size:13px;line-height:2">' + '<br>'.join(log) + '</div>'
    if all_ok:
        html += (f'<div style="margin-top:14px">'
                 f'<form method="post" action="/campaigns/{cid}/start">'
                 f'<button class="btn btn-success" style="padding:12px 24px;font-size:15px">'
                 f'&#9654; Start Mailing</button></form></div>')
    else:
        html += '<div style="margin-top:10px" class="alert alert-warning">Fix issues above before starting.</div>'
    return HTMLResponse(html)


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
    """Send test email using real templates + macros + antifingerprint."""
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

    html_bodies = db.get_all_template_htmls()
    if not html_bodies:
        return HTMLResponse('<div class="alert alert-danger">No HTML templates. Add on HTML Editor page.</div>')

    macros = {}
    for m in db.get_macros():
        md = dict(m)
        lines = [l.strip() for l in (md.get("values_text") or "").splitlines() if l.strip()]
        if lines:
            macros[md["name"]] = lines

    from_name_cfg = cfg.get("from_name", "") or "Test"
    from_email_cfg = cfg.get("from_email", "")
    subject_cfg = cfg.get("subject", "") or "Test"

    import random, smtplib, ssl

    def _process_vars(text, email):
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
        return text

    afp = None
    if cfg.get("advanced_antifingerprint"):
        from mailer.advanced_antifingerprint import AdvancedAntiFingerprintEngine
        afp = AdvancedAntiFingerprintEngine(
            enable_classes=cfg.get("antifingerprint_classes", True),
            structure_variation=cfg.get("structure_variation", 0.5))
    elif cfg.get("antifingerprint_classes"):
        from mailer.antifingerprint import AntiFingerprintEngine
        afp = AntiFingerprintEngine(enable_classes=True)

    # Load redirects + logos for test
    redirect_links = [dict(r)["short_url"] for r in db.get_redirects()] if cfg.get("redirect_enabled") else []

    import glob, os
    logo_files = []
    if cfg.get("image_enabled"):
        from .logos import VARIANT_DIR, UPLOAD_DIR
        logo_files = sorted(glob.glob(os.path.join(VARIANT_DIR, "*")))
        if not logo_files:
            logo_files = sorted(glob.glob(os.path.join(UPLOAD_DIR, "*")))

    results = []
    s = random.choice(smtps)
    from_email = from_email_cfg or s["username"]

    for idx, recipient in enumerate(recipients):
        try:
            html = random.choice(html_bodies)
            html = _process_vars(html, recipient)

            if redirect_links and "{RedirectLink}" in html:
                html = html.replace("{RedirectLink}", redirect_links[idx % len(redirect_links)])

            inline_images = None
            if logo_files and "{Logo}" in html:
                logo_path = random.choice(logo_files)
                try:
                    import mimetypes as mt, secrets as _sec
                    mime_type = mt.guess_type(logo_path)[0] or "image/png"
                    with open(logo_path, "rb") as lf:
                        logo_bytes = lf.read()
                    cid_local = _sec.token_hex(8)
                    cid = f"{cid_local}@{from_email.split('@')[1] if '@' in from_email else 'mail'}"
                    html = html.replace("{Logo}", f'<img src="cid:{cid}" alt="Logo" style="display:block;border:0;max-height:50px;width:auto;">')
                    inline_images = [(logo_bytes, cid, mime_type)]
                except Exception:
                    html = html.replace("{Logo}", "")
            elif "{Logo}" in html:
                html = html.replace("{Logo}", "")

            if afp:
                html = afp.transform(html)
            plain = re.sub(r"<[^>]+>", "", html).strip()
            cur_subject = _process_vars(f"[TEST] {subject_cfg}", recipient)
            cur_from = _process_vars(from_name_cfg, recipient)

            from mailer.mime_builder import MIMEBuilder
            raw_msg = MIMEBuilder.build_email(
                from_name=cur_from, from_email=from_email,
                to_email=recipient, subject=cur_subject,
                html_body=html, plain_body=plain,
                inline_images=inline_images)

            proxy_str = ""
            pv = cfg.get("proxy_value", "").strip()
            if pv:
                proxy_str = pv.splitlines()[0].strip()

            from transactional.web.routes.smtps import _connect_smtp
            server, error, _ = _connect_smtp(s["host"], s["port"], s["username"], s["password"], proxy_str)
            if server:
                server.sendmail(from_email, [recipient], raw_msg)
                server.quit()
                results.append(f'<div style="color:var(--green);font-size:13px">&#10003; Sent to {escape(recipient)}</div>')
            else:
                results.append(f'<div style="color:var(--red);font-size:13px">&#10007; {escape(recipient)}: {escape(error[:100])}</div>')
        except Exception as e:
            results.append(f'<div style="color:var(--red);font-size:13px">&#10007; {escape(recipient)}: {escape(str(e)[:100])}</div>')

    return HTMLResponse("".join(results))


@router.post("/campaigns/{cid}/delete")
async def delete_campaign(request: Request, cid: int):
    _runners.pop(cid, None)
    request.app.state.db.delete_campaign(cid)
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/check-proxy", response_class=HTMLResponse)
async def check_proxy(request: Request, cid: int):
    """Pre-flight proxy connectivity test."""
    db = request.app.state.db
    cfg = db.get_config()
    proxy_value = cfg.get("proxy_value", "").strip()
    if not proxy_value:
        return HTMLResponse('<div class="alert alert-warning">No proxy configured. Set one on the Proxies page.</div>')
    first_proxy = proxy_value.splitlines()[0].strip()
    try:
        import socks
        p = first_proxy.replace("socks5://", "").replace("socks://", "")
        parts = p.split(":")
        if len(parts) < 2:
            return HTMLResponse(f'<div class="alert alert-danger">Invalid proxy format: {escape(first_proxy[:40])}</div>')
        s = socks.socksocket()
        host, port = parts[0], int(parts[1])
        user = parts[2] if len(parts) > 2 else ""
        pwd = parts[3] if len(parts) > 3 else ""
        s.set_proxy(socks.SOCKS5, host, port, username=user or None, password=pwd or None)
        s.settimeout(10)
        s.connect(("api.ipify.org", 80))
        s.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
        resp = s.recv(4096).decode()
        s.close()
        ip = resp.split("\r\n\r\n")[-1].strip() if "\r\n\r\n" in resp else "?"
        return HTMLResponse(f'<div class="alert alert-success">Proxy OK — egress IP: <strong>{escape(ip)}</strong></div>')
    except ImportError:
        return HTMLResponse('<div class="alert alert-danger">PySocks not installed</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">Proxy failed: {escape(str(e)[:100])}</div>')


@router.post("/campaigns/{cid}/check-blacklist", response_class=HTMLResponse)
async def check_blacklist(request: Request, cid: int):
    """MXToolbox blacklist check."""
    db = request.app.state.db
    cfg = db.get_config()
    api_key = cfg.get("mxtoolbox_api_key", "")
    if not api_key:
        return HTMLResponse('<div class="alert alert-warning">No MXToolbox API key in Config.</div>')
    try:
        from mailer.blacklist_checker import BlacklistChecker
        from mailer.smtp_worker import ProxyConfig
        checker = BlacklistChecker(api_key)
        proxy_val = cfg.get("proxy_value", "").strip()
        proxy_objs = None
        if proxy_val:
            proxy_objs = []
            for line in proxy_val.splitlines()[:5]:
                p = ProxyConfig.parse(line.strip())
                if p:
                    proxy_objs.append(p)
        results = checker.check_sending_ips(proxy_objs or None)
        html_parts = []
        for label, info in results.items():
            if info["clean"]:
                html_parts.append(f'<span style="color:var(--green)">&#10003; {escape(label)} clean</span>')
            else:
                count = len(info.get("details", []))
                html_parts.append(f'<span style="color:var(--red)">&#10007; {escape(label)} listed on {count} blacklists</span>')
        return HTMLResponse('<div style="display:flex;flex-wrap:wrap;gap:12px">' + " ".join(html_parts) + '</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">{escape(str(e)[:100])}</div>')


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

    elapsed_str = "—"
    eta_str = "—"
    if cd.get("started_at") and running:
        from datetime import datetime
        try:
            started = datetime.fromisoformat(cd["started_at"])
            elapsed_sec = (datetime.now() - started).total_seconds()
            elapsed_h = int(elapsed_sec // 3600)
            elapsed_m = int((elapsed_sec % 3600) // 60)
            elapsed_str = f"{elapsed_h}h {elapsed_m}m"
            if speed > 0 and remaining > 0:
                eta_sec = remaining / speed * 3600
                eta_h = int(eta_sec // 3600)
                eta_m = int((eta_sec % 3600) // 60)
                eta_str = f"{eta_h}h {eta_m}m"
        except Exception:
            pass

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
        &nbsp; Elapsed: {elapsed_str} &nbsp; ETA: {eta_str}
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
    from datetime import datetime, timedelta

    # Schedule wait
    schedule = camp.get("schedule_time", "")
    if schedule:
        try:
            target_time = datetime.strptime(schedule, "%H:%M").time()
            now = datetime.now()
            target = datetime.combine(now.date(), target_time)
            if target <= now:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            logger.info("Campaign %d: scheduled for %s (waiting %ds)", cid, target, int(wait))
            db.update_campaign(cid, status="SCHEDULED")
            while wait > 0 and cid in _runners:
                time.sleep(min(wait, 5))
                wait = (target - datetime.now()).total_seconds()
        except ValueError:
            pass

    if cid not in _runners:
        return

    db.update_campaign(cid, status="RUNNING", started_at=datetime.now().isoformat())

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

        html_bodies = db.get_all_template_htmls()

        macros = {}
        for m in db.get_macros():
            md = dict(m)
            lines = [l.strip() for l in (md.get("values_text") or "").splitlines() if l.strip()]
            if lines:
                macros[md["name"]] = lines

        from_name_cfg = cfg.get("from_name", "") or "Newsletter"
        from_email_cfg = cfg.get("from_email", "")
        subject_cfg = cfg.get("subject", "") or "Notification"

        # Logo setup
        logo_variants = []
        if cfg.get("image_enabled"):
            import glob
            from .logos import VARIANT_DIR, UPLOAD_DIR
            variant_files = sorted(glob.glob(os.path.join(VARIANT_DIR, "*")))
            if variant_files:
                logo_variants = variant_files
                logger.info("Campaign %d: %d logo variants loaded", cid, len(logo_variants))
            else:
                source_files = sorted(glob.glob(os.path.join(UPLOAD_DIR, "*")))
                if source_files:
                    logo_variants = source_files
                    logger.info("Campaign %d: %d source logos (no variants)", cid, len(source_files))

        # Redirect setup
        redirect_links = []
        if cfg.get("redirect_enabled"):
            redirects = db.get_redirects()
            redirect_links = [dict(r)["short_url"] for r in redirects]
            logger.info("Campaign %d: %d redirect links loaded", cid, len(redirect_links))
        redirect_rotate = cfg.get("redirect_rotate_every", 10) or 10

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

        campaign_id = cid

        def _send_one(lead_id, email):
            nonlocal sent, failed
            logger.info("Campaign %d: _send_one for lead %d (%s)", campaign_id, lead_id, email)
            if campaign_id not in _runners:
                return
            account = pool.acquire()
            if account is None:
                if pool.all_dead:
                    logger.error("Campaign %d: all SMTPs dead", campaign_id)
                    with _lock:
                        db.mark_failed(lead_id, "All SMTPs dead")
                        failed += 1
                        db.update_campaign(campaign_id, sent=sent, failed=failed)
                    return
                time.sleep(3)
                account = pool.acquire()
                if account is None:
                    return

            cur_from_email = from_email_cfg or account.user
            try:
                cur_from_name = _process(from_name_cfg, email)
                cur_subject = _process(subject_cfg, email)

                send_idx = sent + failed
                html = random.choice(html_bodies) if html_bodies else "<p>Hello {email_user}</p>"
                html = _process(html, email)

                # Resolve {RedirectLink}
                if redirect_links and "{RedirectLink}" in html:
                    group = send_idx // redirect_rotate
                    link = redirect_links[group % len(redirect_links)] if redirect_links else ""
                    html = html.replace("{RedirectLink}", link)
                    cur_subject = cur_subject.replace("{RedirectLink}", link)

                # Resolve {Logo} — CID inline
                inline_images = None
                if logo_variants and "{Logo}" in html:
                    logo_path = random.choice(logo_variants)
                    try:
                        import mimetypes as mt
                        mime_type = mt.guess_type(logo_path)[0] or "image/png"
                        with open(logo_path, "rb") as lf:
                            logo_bytes = lf.read()
                        import secrets as _sec
                        cid_local = _sec.token_hex(8)
                        domain_part = (cur_from_email.split("@")[1] if "@" in cur_from_email else "mail")
                        cid = f"{cid_local}@{domain_part}"
                        html = html.replace("{Logo}",
                            f'<img src="cid:{cid}" alt="Logo" style="display:block;border:0;max-height:50px;width:auto;">')
                        inline_images = [(logo_bytes, cid, mime_type)]
                    except Exception as le:
                        logger.warning("Logo embed error: %s", le)
                        html = html.replace("{Logo}", "")
                elif "{Logo}" in html:
                    html = html.replace("{Logo}", "")

                if afp:
                    html = afp.transform(html)
                plain = re.sub(r"<br\s*/?>", "\n", html)
                plain = re.sub(r"<[^>]+>", "", plain).strip()

                raw_msg = MIMEBuilder.build_email(
                    from_name=cur_from_name, from_email=cur_from_email,
                    to_email=email, subject=cur_subject,
                    html_body=html, plain_body=plain,
                    inline_images=inline_images)

            except Exception as build_exc:
                logger.error("Campaign %d BUILD error for %s: %s", campaign_id, email, build_exc, exc_info=True)
                with _lock:
                    db.mark_failed(lead_id, f"BUILD: {str(build_exc)[:400]}")
                    failed += 1
                    db.update_campaign(campaign_id, sent=sent, failed=failed)
                return

            try:
                result = worker.send(cur_from_email, email, raw_msg, account=account)
            except Exception as send_exc:
                logger.error("Campaign %d send exception for %s: %s", campaign_id, email, send_exc, exc_info=True)
                with _lock:
                    db.mark_failed(lead_id, str(send_exc)[:500])
                    failed += 1
                    db.update_campaign(campaign_id, sent=sent, failed=failed)
                return

            with _lock:
                if result.is_success:
                    db.mark_sent(lead_id)
                    sent += 1
                elif result.is_fatal:
                    db.mark_failed(lead_id, result.error[:500])
                    failed += 1
                    logger.warning("Campaign %d FATAL for %s: %s", campaign_id, email, result.error[:200])
                else:
                    db._conn().execute("UPDATE trans_leads SET state='PENDING' WHERE id=?", (lead_id,))
                    db._conn().commit()
                    logger.warning("Campaign %d transient for %s: %s", campaign_id, email, result.error[:200])
                db.update_campaign(campaign_id, sent=sent, failed=failed)

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
                        lid = futures[f]
                        logger.error("Campaign %d EXECUTOR error for lead %d: %s", cid, lid, e, exc_info=True)
                        with _lock:
                            db.mark_failed(lid, str(e)[:500])
                            failed += 1

            # Retry failed leads once
            if cid in _runners and not pool.all_dead:
                failed_count = db._conn().execute(
                    "SELECT COUNT(*) FROM trans_leads WHERE list_id=? AND state='FAILED'",
                    (lead_list_id,)).fetchone()[0]
                if failed_count > 0:
                    logger.info("Campaign %d: retrying %d failed leads", cid, failed_count)
                    db._conn().execute(
                        "UPDATE trans_leads SET state='PENDING' WHERE list_id=? AND state='FAILED'",
                        (lead_list_id,))
                    db._conn().commit()

                    while cid in _runners:
                        if pool.all_dead:
                            break
                        batch = db.fetch_pending(lead_list_id, 200)
                        if not batch:
                            break
                        db.mark_in_progress([r["id"] for r in batch])
                        futs = {executor.submit(_send_one, r["id"], r["email"]): r["id"] for r in batch}
                        for f in as_completed(futs):
                            try:
                                f.result(timeout=120)
                            except Exception as e:
                                logger.error("Campaign %d RETRY error: %s", cid, e)

        status = "FINISHED" if cid not in _runners else "PAUSED"
        from datetime import datetime
        logger.info("Campaign %d %s: sent=%d, failed=%d", cid, status, sent, failed)
        db.update_campaign(cid, status=status, sent=sent, failed=failed,
                           finished_at=datetime.now().isoformat())
        db.reset_in_progress(lead_list_id)

        if status == "FINISHED":
            from .logos import clear_variants
            clear_variants()
            logger.info("Campaign %d: logo variants cleared", cid)

    finally:
        os.unlink(smtp_file.name)
        if proxy_file_path:
            os.unlink(proxy_file_path)
