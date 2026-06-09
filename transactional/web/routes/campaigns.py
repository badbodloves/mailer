"""Campaigns — CRUD, start/stop, live stats."""
import re
import time
import random
import logging
import threading
from html import escape
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("trans.campaigns")
router = APIRouter()


def _append_ref(url: str, email: str) -> str:
    """Append ?ref=email (URL-encoded) to a redirect link. Uses & if the
    URL already has a query string. @ . + - _ are kept literal so the
    address remains human-readable in tracking logs."""
    if not url or not email:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}ref={quote(email, safe='@.+-_')}"


_runners = {}
_speed = {}


def _enrich(db, cd):
    cd["running"] = cd["id"] in _runners
    cd["speed"] = _speed.get(cd["id"], 0)
    total = cd.get("total_leads", 0) or 0
    sent = cd.get("sent", 0) or 0
    failed = cd.get("failed", 0) or 0
    cd["pct"] = int(sent / total * 100) if total > 0 else 0
    cd["remaining"] = max(0, total - sent)
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
    uid = request.state.user["id"]
    campaigns = [_enrich(db, dict(c)) for c in db.get_campaigns(uid)]
    smtp_lists = [dict(sl, count=db.get_smtp_count(sl["id"])) for sl in db.get_smtp_lists(uid)]
    lead_lists = [dict(ll, count=db.get_lead_count(ll["id"])) for ll in db.get_lead_lists(uid)]
    pools = [dict(p, stats=db.pool_stats(p["id"])) for p in db.get_pools(uid)]
    templates = [dict(t) for t in db.get_templates(uid)]
    redirect_pools = [dict(p, count=db.get_redirect_pool_count(p["id"])) for p in db.get_redirect_pools(uid)]
    return request.app.state.templates.TemplateResponse(request, "campaigns.html", {
        "active": "campaigns", "campaigns": campaigns,
        "smtp_lists": smtp_lists, "lead_lists": lead_lists,
        "pools": pools, "templates": templates,
        "redirect_pools": redirect_pools, "db": db,
    })


@router.post("/campaigns/add")
async def add_campaign(request: Request,
                       name: str = Form(""),
                       smtp_list_id: int = Form(0),
                       lead_list_id: int = Form(0),
                       pool_id: int = Form(0),
                       pool_count: int = Form(0)):
    db = request.app.state.db
    uid = request.state.user['id']

    if not smtp_list_id:
        return RedirectResponse("/campaigns", status_code=303)

    # Pool mode: reserve N leads from pool into a temp lead list
    if pool_id and pool_count > 0:
        stats = db.pool_stats(pool_id)
        available = stats["pending"]
        take = min(pool_count, available)
        if take == 0:
            return RedirectResponse("/campaigns", status_code=303)
        # Create a temp lead list from pool leads
        pool_row = db._conn().execute("SELECT name FROM trans_lead_lists WHERE id=?", (pool_id,)).fetchone()
        pool_name = pool_row["name"] if pool_row else "Pool"
        temp_list_id = db.create_lead_list(
            f"{pool_name} ({take:,} leads)", "", uid)
        # Copy next N pending leads from pool to temp list
        c = db._conn()
        pending = c.execute(
            "SELECT id, email FROM trans_leads WHERE list_id=? AND state='PENDING' ORDER BY id LIMIT ?",
            (pool_id, take)).fetchall()
        batch = [(temp_list_id, r["email"]) for r in pending]
        c.executemany("INSERT INTO trans_leads (list_id,email,state) VALUES (?,?,'PENDING')", batch)
        # Mark pool leads as USED
        ids = [r["id"] for r in pending]
        ph = ",".join("?" for _ in ids)
        c.execute(f"UPDATE trans_leads SET state='USED' WHERE id IN ({ph})", ids)
        c.execute("UPDATE trans_lead_lists SET lead_count=? WHERE id=?", (take, temp_list_id))
        c.commit()
        lead_list_id = temp_list_id
        total = take
    elif lead_list_id:
        total = db.get_lead_count(lead_list_id)
    else:
        return RedirectResponse("/campaigns", status_code=303)

    db.create_campaign(
        name=name.strip() or f"Campaign {time.strftime('%Y-%m-%d %H:%M')}",
        smtp_list_id=smtp_list_id, lead_list_id=lead_list_id,
        total_leads=total, user_id=uid)
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/campaigns/{cid}/save")
async def save_campaign(request: Request, cid: int,
                        name: str = Form(""),
                        smtp_list_id: int = Form(0),
                        lead_list_id: int = Form(0),
                        template_id: int = Form(0),
                        redirect_pool_id: int = Form(0),
                        schedule_time: str = Form("")):
    db = request.app.state.db
    updates = {"name": name.strip(), "schedule_time": schedule_time.strip(),
               "template_id": template_id, "redirect_pool_id": redirect_pool_id}
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
    uid = request.state.user['id']
    camp = db.get_campaign(cid)
    if not camp:
        return HTMLResponse('<div class="alert alert-danger">Campaign not found.</div>')
    camp = dict(camp)
    cfg = db.get_config()
    log = []
    all_ok = True

    # 0. Check proxy
    proxy_val = cfg.get("proxy_value", "").strip()
    if proxy_val:
        log.append(f'<span style="color:var(--green)">&#10003; Proxy active: {escape(proxy_val.splitlines()[0][:40])}</span>')
    else:
        log.append('<span style="color:var(--red);font-weight:600">&#9888; NO PROXY — your server IP will be exposed in email headers!</span>')
        all_ok = False

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
        count = db.get_redirect_count(uid)
        if count > 0:
            log.append(f'<span style="color:var(--green)">&#10003; {count} redirect links in pool</span>')
        else:
            log.append('<span style="color:var(--red)">&#10007; No redirect links. <a href="/redirects" style="color:var(--accent)">Generate</a></span>')
            all_ok = False
    else:
        log.append('<span style="color:var(--fg2)">Redirect links disabled</span>')

    # 3. Check templates
    html_count = len(db.get_all_template_htmls(uid))
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


@router.post("/campaigns/{cid}/resend-failed")
async def resend_failed(request: Request, cid: int):
    """Reset failed leads to PENDING, then start campaign."""
    db = request.app.state.db
    camp = db.get_campaign(cid)
    if not camp:
        return RedirectResponse("/campaigns", status_code=303)
    camp = dict(camp)
    lead_list_id = camp.get("lead_list_id", 0)
    if lead_list_id:
        count = db._conn().execute(
            "SELECT COUNT(*) FROM trans_leads WHERE list_id=? AND state='FAILED'",
            (lead_list_id,)).fetchone()[0]
        if count > 0:
            db._conn().execute(
                "UPDATE trans_leads SET state='PENDING' WHERE list_id=? AND state='FAILED'",
                (lead_list_id,))
            db._conn().commit()
            logger.info("Campaign %d: reset %d failed leads to PENDING", cid, count)
    if cid not in _runners:
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


@router.post("/campaigns/{cid}/test-send", response_class=HTMLResponse)
async def test_send(request: Request, cid: int):
    """Send test email using real templates + macros + antifingerprint."""
    db = request.app.state.db
    cfg = db.get_config()
    recipients_raw = cfg.get("test_recipients", "")
    if not recipients_raw.strip():
        return HTMLResponse('<div class="alert alert-warning">No test recipients in Config.</div>')

    uid = request.state.user['id']
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    camp = db.get_campaign(cid)
    if not camp:
        return HTMLResponse('<div class="alert alert-danger">Campaign not found.</div>')
    camp = dict(camp)

    smtp_list_id = camp.get("smtp_list_id", 0)
    smtps = [dict(s) for s in db.get_smtps(smtp_list_id)] if smtp_list_id else []
    if not smtps:
        return HTMLResponse('<div class="alert alert-danger">No SMTPs in selected list.</div>')

    html_bodies = db.get_all_template_htmls(uid)
    if not html_bodies:
        return HTMLResponse('<div class="alert alert-danger">No HTML templates. Add on HTML Editor page.</div>')

    macros = {}
    for m in db.get_macros(uid):
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
    redirect_links = [dict(r)["short_url"] for r in db.get_redirects(uid)] if cfg.get("redirect_enabled") else []

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
                link = redirect_links[idx % len(redirect_links)]
                if cfg.get("redirect_append_ref"):
                    link = _append_ref(link, recipient)
                html = html.replace("{RedirectLink}", link)

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
    remaining = max(0, total - sent)
    pct = int(sent / total * 100) if total > 0 else 0
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
    uid = request.state.user['id']
    campaigns = [_enrich(db, dict(c)) for c in db.get_campaigns(uid)]
    html = f'<div class="card-header"><h3>Campaigns <span class="count">{len(campaigns)}</span></h3></div>'
    html += '<table><thead><tr><th>Name</th><th>SMTP</th><th>Leads</th><th>Progress</th><th>Total</th><th>Sent</th><th>Speed</th><th></th></tr></thead><tbody>'
    for c in campaigns:
        bg = ' style="background:var(--green-light)"' if c["running"] else ""
        speed = f'{c["speed"]:,}/h' if c["speed"] else "—"
        start_dis = ' disabled' if c["running"] else ''
        pause_dis = '' if c["running"] else ' disabled'
        btn = (f'<form method="post" action="/campaigns/{c["id"]}/start" style="display:inline">'
               f'<button class="btn btn-success btn-xs"{start_dis}>Start</button></form>'
               f'<form method="post" action="/campaigns/{c["id"]}/stop" style="display:inline">'
               f'<button class="btn btn-danger btn-xs"{pause_dis}>Pause</button></form>')
        html += (f'<tr{bg}><td style="font-weight:500">{c["name"]}</td>'
                 f'<td style="font-size:12px">{c["smtp_list_name"]}</td>'
                 f'<td style="font-size:12px">{c["lead_list_name"]}</td>'
                 f'<td style="min-width:100px"><div class="progress"><div class="progress-bar" style="width:{c["pct"]}%">{c["pct"]}%</div></div></td>'
                 f'<td>{c.get("total_leads",0) or 0:,}</td>'
                 f'<td style="color:var(--green)">{c.get("sent",0) or 0:,}</td>'
                 f'<td>{speed}</td><td style="white-space:nowrap">{btn}</td></tr>')
    html += '</tbody></table>'
    return HTMLResponse(html)


@router.post("/campaigns/{cid}/spam-check", response_class=HTMLResponse)
async def spam_check_campaign(request: Request, cid: int):
    """Build a sample email and check spam score."""
    db = request.app.state.db
    uid = request.state.user["id"]
    cfg = db.get_config()

    html_bodies = db.get_all_template_htmls(uid)
    if not html_bodies:
        return HTMLResponse('<div class="alert alert-warning">No templates to check.</div>')

    html = html_bodies[0]
    html = html.replace("{email}", "test@example.com").replace("{email_user}", "test")
    html = html.replace("{domain}", "example.com").replace("{Logo}", "").replace("{RedirectLink}", "https://example.com")
    plain = re.sub(r"<[^>]+>", "", html).strip()
    from_email = cfg.get("from_email", "") or "test@example.com"

    try:
        from mailer.mime_builder import MIMEBuilder
        raw_msg = MIMEBuilder.build_email(
            from_name=cfg.get("from_name", "Test"), from_email=from_email,
            to_email="check@example.com", subject=cfg.get("subject", "Test"),
            html_body=html, plain_body=plain)
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">Build: {escape(str(e)[:100])}</div>')

    from .spam_check import check_spam, format_result_html
    result = check_spam(raw_msg, cfg.get("spam_checker", "rspamd"),
                         cfg.get("spam_checker_url", "http://127.0.0.1:11333/checkv2"))
    return HTMLResponse(format_result_html(result))


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

    uid = camp.get("user_id", 0)
    lead_list_id = camp.get("lead_list_id", 0)
    smtp_list_id = camp.get("smtp_list_id", 0)

    if not lead_list_id or not smtp_list_id:
        logger.error("Campaign %d: no SMTP or lead list", cid)
        db.update_campaign(cid, status="FAILED")
        return

    db.reset_in_progress(lead_list_id)
    states = db.get_lead_states(lead_list_id)
    pending = states.get("PENDING", 0)
    failed_leads = states.get("FAILED", 0)
    prev_status = camp.get("status", "DRAFT")
    is_resume = prev_status in ("PAUSED", "FINISHED", "RUNNING")

    if pending == 0 and not is_resume:
        total = sum(states.values())
        if total > 0:
            logger.info("Campaign %d: 0 pending (fresh start), resetting %d leads", cid, total)
            db.reset_leads(lead_list_id)
            pending = total
    elif pending == 0 and is_resume:
        if failed_leads > 0 and cfg.get("auto_retry_failed", True):
            logger.info("Campaign %d: resumed, retrying %d failed leads", cid, failed_leads)
            db._conn().execute(
                "UPDATE trans_leads SET state='PENDING' WHERE list_id=? AND state='FAILED'",
                (lead_list_id,))
            db._conn().commit()
            pending = failed_leads
        else:
            logger.info("Campaign %d: resumed but 0 pending — all leads already processed", cid)
            db.update_campaign(cid, status="FINISHED")
            return

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

        campaign_template_id = camp.get("template_id", 0) or 0
        # html_bodies + logo_variants are kept as mutable lists so the
        # freshness monitor can swap their contents in-place during the
        # send (workers read via random.choice which sees the swap).
        html_bodies = list(db.get_all_template_htmls(uid, template_id=campaign_template_id))

        macros = {}
        for m in db.get_active_macros(uid):
            md = dict(m)
            lines = [l.strip() for l in (md.get("values_text") or "").splitlines() if l.strip()]
            if lines:
                macros[md["name"]] = lines

        from_name_cfg = cfg.get("from_name", "") or "Newsletter"
        from_email_cfg = cfg.get("from_email", "")
        subject_cfg = cfg.get("subject", "") or "Notification"

        # Logo setup — load from template's logo group variant dir.
        # logo_variants stays mutable so freshness reset can swap it.
        logo_variants = []
        logo_cdn_urls = []
        logo_group_id = 0
        group_logos_for_freshness = []   # source logos so freshness can re-derive
        _group_variant_dir = None        # only defined when image_enabled
        if cfg.get("image_enabled"):
            import glob
            from .logos import VARIANT_DIR, UPLOAD_DIR, _group_variant_dir, _resolve_path as _resolve_logo
            if campaign_template_id:
                tpl_row = db.get_template(campaign_template_id)
                if tpl_row:
                    logo_group_id = dict(tpl_row).get("logo_group_id", 0) or 0

            if logo_group_id:
                gdir = _group_variant_dir(logo_group_id)
                variant_files = sorted(glob.glob(os.path.join(gdir, "*")))
                if variant_files:
                    logo_variants = list(variant_files)
                    logger.info("Campaign %d: %d variants from group %d", cid, len(logo_variants), logo_group_id)
                else:
                    group_logos = db.get_logos_by_group(logo_group_id)
                    logo_variants = [_resolve_logo(dict(l)["file_path"]) for l in group_logos]
                    logo_variants = [p for p in logo_variants if os.path.isfile(p)]
                    logger.info("Campaign %d: %d source logos from group %d (no variants)", cid, len(logo_variants), logo_group_id)
                # Cache source logos as dicts so freshness can pass them in
                try:
                    group_logos_for_freshness = [dict(l) for l in db.get_logos_by_group(logo_group_id)]
                except Exception:
                    group_logos_for_freshness = []

            if not logo_variants:
                variant_files = sorted(glob.glob(os.path.join(VARIANT_DIR, "v_*")))
                if variant_files:
                    logo_variants = variant_files
                else:
                    source_files = sorted(glob.glob(os.path.join(UPLOAD_DIR, "*")))
                    if source_files:
                        logo_variants = source_files
                logger.info("Campaign %d: %d logos (global fallback)", cid, len(logo_variants))

            # Load Cloudinary CDN URLs if mode is cloudinary
            if cfg.get("image_mode") == "cloudinary":
                import json as _json
                if logo_group_id:
                    grp = db._conn().execute("SELECT cdn_urls_json FROM trans_logo_groups WHERE id=?",
                                              (logo_group_id,)).fetchone()
                    if grp and grp["cdn_urls_json"]:
                        logo_cdn_urls = _json.loads(grp["cdn_urls_json"])
                if not logo_cdn_urls:
                    for g in db.get_logo_groups(uid):
                        gd = dict(g)
                        if gd.get("cdn_urls_json"):
                            logo_cdn_urls.extend(_json.loads(gd["cdn_urls_json"]))
                logger.info("Campaign %d: %d Cloudinary URLs loaded", cid, len(logo_cdn_urls))

        # Redirect setup
        redirect_links = []
        redirect_pool_id = camp.get("redirect_pool_id", 0) or 0
        if redirect_pool_id:
            redirects = db.get_redirects_by_pool(redirect_pool_id)
            redirect_links = [dict(r)["short_url"] for r in redirects]
        elif cfg.get("redirect_enabled"):
            redirects = db.get_redirects(uid)
            redirect_links = [dict(r)["short_url"] for r in redirects]
        if redirect_links:
            logger.info("Campaign %d: %d redirect links loaded", cid, len(redirect_links))
        mime_profile_mode = cfg.get("mime_profile", "default")


        def _classify_error(error_str: str, code: int = 0) -> str:
            e = error_str.lower()

            # ============================================================
            # SMTP-ACCOUNT FAILURES — these kill the SMTP, NOT the lead.
            # Must be checked FIRST so phrases like "SMTP Blocked" don't
            # accidentally fall into the generic "blocked = spam_reject"
            # branch further down.
            # ============================================================

            # --- Authentication (fatal: password is wrong) ---
            if "incorrect authentication" in e or "authentication failed" in e:
                return "auth_fail"
            if "535" in e and ("auth" in e or "login" in e or "credentials" in e):
                return "auth_fail"
            if "invalid login" in e or "username and password not accepted" in e:
                return "auth_fail"

            # --- Account-level block: provider says "you can't send" ---
            if "smtp blocked" in e or "sender blocked" in e or "sender rejected" in e:
                return "smtp_blocked"
            if "outbound blocked" in e or "outgoing blocked" in e:
                return "smtp_blocked"
            if "from address rejected" in e or "envelope sender" in e and "reject" in e:
                return "smtp_blocked"
            if "postmaster" in e and ("reject" in e or "block" in e or "denied" in e):
                return "smtp_blocked"
            if "suspended" in e or "deactivated" in e:
                return "smtp_suspended"
            if "outgoing" in e and ("blocked" in e or "suspended" in e or "disabled" in e):
                return "smtp_suspended"
            if "account" in e and ("disabled" in e or "blocked" in e or "frozen" in e):
                return "smtp_suspended"

            # --- Rate limiting (SMTP-side, but recoverable after cooldown) ---
            if "too many" in e or ("rate" in e and "limit" in e) or "throttl" in e:
                return "rate_limit"
            if "has sent too many" in e or "sending rate" in e or "message rate" in e:
                return "rate_limit"
            if "try again later" in e or "too many connections" in e:
                return "rate_limit"

            # --- Network/transport (transient, suspend SMTP briefly) ---
            if "timeout" in e or "timed out" in e:
                return "timeout"
            if "ssl" in e and ("legacy" in e or "renegotiation" in e or "handshake" in e or "certificate" in e or "unsafe" in e):
                return "connection"
            if "ssl" in e and "error" in e:
                return "connection"
            if "connect" in e and ("refused" in e or "error" in e or "reset" in e):
                return "connection"
            if "eof" in e or "broken pipe" in e or "connection reset" in e or "unexpectedly closed" in e:
                return "connection"
            # smtplib raises this when the socket died mid-session — the
            # connection is gone, the SMTP server is fine. Worker needs
            # to drop this connection and acquire a fresh one.
            if "please run connect" in e or "smtpserverdisconnected" in e:
                return "connection"
            if "server not connected" in e or "not connected" in e:
                return "connection"

            # ============================================================
            # RECIPIENT-LEVEL FAILURES — SMTP is fine, just this address
            # didn't work. Worker keeps the connection and moves on.
            # ============================================================

            if "recipients refused" in e:
                inner = e.split("(", 1)
                if len(inner) > 1:
                    inner_msg = inner[1]
                    # Re-check SMTP-side in the inner so account problems
                    # nested inside SMTPRecipientsRefused still kill the
                    # SMTP instead of looking like a recipient bounce.
                    if "auth" in inner_msg or "535" in inner_msg or "login" in inner_msg:
                        return "auth_fail"
                    if "suspend" in inner_msg or "deactivat" in inner_msg or "disabled" in inner_msg:
                        return "smtp_suspended"
                    if "smtp blocked" in inner_msg or "sender blocked" in inner_msg or "sender rejected" in inner_msg:
                        return "smtp_blocked"
                    if "too many" in inner_msg or "rate" in inner_msg or "limit" in inner_msg:
                        return "rate_limit"
                    if "spam" in inner_msg or "blacklist" in inner_msg or "dnsbl" in inner_msg:
                        return "spam_reject"
                    if "no such user" in inner_msg or "mailbox not found" in inner_msg or \
                       "does not exist" in inner_msg or "unknown user" in inner_msg or \
                       "user unknown" in inner_msg or "invalid recipient" in inner_msg or \
                       "recipient rejected" in inner_msg or "addressee unknown" in inner_msg:
                        return "mailbox_not_found"
                    if "blocked" in inner_msg or "rejected" in inner_msg:
                        return "spam_reject"
                return "spam_reject"

            # Mailbox truly doesn't exist
            if "no such user" in e or "mailbox not found" in e or \
               "does not exist" in e or "unknown user" in e or \
               "user unknown" in e or "invalid recipient" in e or \
               "addressee unknown" in e or "recipient rejected" in e:
                return "mailbox_not_found"

            # Spam/content rejection (recipient-side content decision)
            if "spam" in e or "dnsbl" in e or "blacklist" in e:
                return "spam_reject"
            if ("policy" in e and "reject" in e) or ("content" in e and "reject" in e):
                return "spam_reject"

            # Generic "blocked" — ambiguous, but if we reach this point the
            # account-specific patterns above didn't match, so it's
            # probably a recipient/content block.
            if "blocked" in e:
                return "spam_reject"

            # --- SMTP code fallbacks ---
            if code >= 550:
                return "permanent_reject"
            if code >= 400:
                return "rate_limit"

            return "other"


        # Which error classes kill the SMTP vs. just the lead.
        SMTP_FATAL     = {"auth_fail", "smtp_suspended", "smtp_blocked"}
        SMTP_TRANSIENT = {"rate_limit", "connection", "timeout"}

        def _log_bounce(lead_id, email, error_str, code=0, smtp_host="", smtp_user=""):
            etype = _classify_error(error_str, code)
            profile = mime_profile_mode if mime_profile_mode != "rotate" else "rotated"
            db.log_bounce(campaign_id, lead_id, email, code, etype,
                          error_str[:500], profile, smtp_host, smtp_user, uid)

        thread_count = min(cfg.get("threads", 40), pool.size * 2, 200)
        thread_count = max(thread_count, 1)
        logger.info("Campaign %d starting: %d pending, %d SMTPs, %d threads",
                     cid, pending, pool.size, thread_count)

        sent = 0
        failed = 0
        _lock = threading.Lock()

        # Freshness reset barrier — workers pause here while a refresh
        # of the HTML pool and/or logo variant set happens mid-send.
        freshness_barrier = threading.Event()
        freshness_barrier.set()
        freshness_every = int(cfg.get("freshness_every_n_mails", 0) or 0)
        freshness_html  = bool(cfg.get("freshness_reset_html", False))
        freshness_logos = bool(cfg.get("freshness_reset_logos", False))

        def _freshness_monitor():
            """Watches the global `sent` counter and triggers a refresh
            every `freshness_every` successful mails. Runs in its own
            daemon thread; failures are logged but never bubble up."""
            if freshness_every <= 0 or not (freshness_html or freshness_logos):
                return
            next_reset_at = freshness_every
            while campaign_id in _runners:
                time.sleep(2)
                with _lock:
                    cur_sent = sent
                if cur_sent < next_reset_at:
                    continue
                logger.info("Campaign %d: freshness reset triggered at sent=%d",
                             cid, cur_sent)
                freshness_barrier.clear()
                # Give in-flight builds a moment to finish before we swap
                time.sleep(0.5)
                try:
                    if freshness_html:
                        new_bodies = []
                        from mailer.freshness import regenerate_html_pool
                        try:
                            new_bodies = regenerate_html_pool(
                                count=int(cfg.get("freshness_html_count", 25) or 25))
                        except Exception as e:
                            logger.warning("Campaign %d: html regen failed: %s", cid, e)
                        if new_bodies:
                            html_bodies.clear()
                            html_bodies.extend(new_bodies)
                            logger.info("Campaign %d: html pool refreshed (%d new bodies)",
                                         cid, len(new_bodies))

                    if freshness_logos and logo_group_id and group_logos_for_freshness:
                        # Logo regen only meaningfully helps modes that
                        # actually read logo_variants at send time. In
                        # text and cloudinary modes the regenerated
                        # files are never touched, so we skip the work.
                        if image_mode not in ("cid", "url"):
                            logger.info(
                                "Campaign %d: skipping logo regen "
                                "(image_mode=%s doesn't use local variants)",
                                cid, image_mode,
                            )
                        else:
                            from mailer.freshness import regenerate_logo_variants
                            try:
                                gdir = _group_variant_dir(logo_group_id)
                                new_vars = regenerate_logo_variants(
                                    group_logos_for_freshness,
                                    gdir,
                                    int(cfg.get("freshness_logo_count", 25) or 25),
                                    max_colors=int(cfg.get("logo_max_colors", 256) or 256),
                                    quantize=bool(cfg.get("image_quantize", True)),
                                    downscale=bool(cfg.get("image_downscale", False)),
                                )
                                if new_vars:
                                    logo_variants.clear()
                                    logo_variants.extend(new_vars)
                                    logger.info("Campaign %d: logo variants refreshed (%d new)",
                                                 cid, len(new_vars))
                            except Exception as e:
                                logger.warning("Campaign %d: logo regen failed: %s", cid, e)
                except Exception as e:
                    logger.error("Campaign %d: freshness reset crashed: %s", cid, e)
                finally:
                    next_reset_at = cur_sent + freshness_every
                    freshness_barrier.set()

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
        threading.Thread(target=_freshness_monitor, daemon=True).start()

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
        import queue
        mail_queue = queue.Queue()
        image_mode = cfg.get("image_mode", "cid")

        def _build_and_send(server_obj, account, lead_id, email):
            """Build email and send over existing connection. Returns True on success."""
            nonlocal sent, failed
            cur_from_email = from_email_cfg or account.user
            try:
                cur_from_name = _process(from_name_cfg, email)
                cur_subject = _process(subject_cfg, email)
                html = random.choice(html_bodies) if html_bodies else "<p>Hello {email_user}</p>"
                html = _process(html, email)

                if redirect_links and "{RedirectLink}" in html:
                    link = random.choice(redirect_links)
                    if cfg.get("redirect_append_ref"):
                        link = _append_ref(link, email)
                    html = html.replace("{RedirectLink}", link)
                    cur_subject = cur_subject.replace("{RedirectLink}", link)

                inline_images = None
                if "{Logo}" in html:
                    if image_mode == "text":
                        logo_text = _process(cfg.get("logo_text", "{Logo}"), email)
                        html = html.replace("{Logo}",
                            f'<span style="font-weight:bold;font-size:16px;color:{cfg.get("logo_text_color", "#333333")};">{logo_text}</span>')
                    elif image_mode == "cloudinary" and logo_cdn_urls:
                        html = html.replace("{Logo}",
                            f'<img src="{random.choice(logo_cdn_urls)}" alt="Logo" style="display:block;border:0;max-height:50px;width:auto;">')
                    elif image_mode == "url" and logo_variants:
                        base = cfg.get("logo_base_url", "").rstrip("/")
                        html = html.replace("{Logo}",
                            f'<img src="{base}/{os.path.basename(random.choice(logo_variants))}" alt="Logo" style="display:block;border:0;max-height:50px;width:auto;">' if base else "")
                    elif logo_variants:
                        logo_path = random.choice(logo_variants)
                        try:
                            import mimetypes as mt
                            mime_type = mt.guess_type(logo_path)[0] or "image/png"
                            with open(logo_path, "rb") as lf:
                                logo_bytes = lf.read()
                            import secrets as _sec
                            cid_val = f"{_sec.token_hex(8)}@{cur_from_email.split('@')[1] if '@' in cur_from_email else 'mail'}"
                            html = html.replace("{Logo}",
                                f'<img src="cid:{cid_val}" alt="Logo" style="display:block;border:0;max-height:50px;width:auto;">')
                            inline_images = [(logo_bytes, cid_val, mime_type)]
                        except Exception:
                            html = html.replace("{Logo}", "")
                    else:
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

                if mime_profile_mode == "rotate":
                    from mailer.mime_profiles import get_random_profile, apply_profile
                    raw_msg = apply_profile(raw_msg, get_random_profile(), cur_from_email)
                elif mime_profile_mode != "default":
                    from mailer.mime_profiles import apply_profile
                    raw_msg = apply_profile(raw_msg, mime_profile_mode, cur_from_email)

            except Exception as build_exc:
                with _lock:
                    db.mark_failed(lead_id, f"BUILD: {str(build_exc)[:400]}")
                    failed += 1
                return True

            try:
                server_obj.sendmail(cur_from_email, email, raw_msg)
            except Exception as send_exc:
                # Pull the SMTP code if smtplib gave us one (sendmail
                # raises SMTPRecipientsRefused with a dict of code/msg).
                code = 0
                if hasattr(send_exc, "smtp_code"):
                    code = getattr(send_exc, "smtp_code", 0) or 0
                elif hasattr(send_exc, "recipients"):
                    try:
                        first = next(iter(send_exc.recipients.values()))
                        code = first[0] if isinstance(first, tuple) else 0
                    except Exception:
                        code = 0

                err_str = str(send_exc)
                etype = _classify_error(err_str, code)

                with _lock:
                    db.log_bounce(campaign_id, lead_id, email, code, etype,
                                  err_str[:500],
                                  mime_profile_mode if mime_profile_mode != "rotate" else "rotated",
                                  account.host, account.user, uid)
                    db.mark_failed(lead_id, err_str[:500])
                    failed += 1

                if etype in SMTP_FATAL:
                    # SMTP account is broken — kick it out of the pool
                    # immediately, do not pick it again this campaign.
                    pool.mark_dead(account, f"{etype}: {err_str[:200]}")
                    raise Exception(send_exc)
                if etype in SMTP_TRANSIENT:
                    # Transient: cooldown via the pool's escalating backoff.
                    pool.suspend(account, etype)
                    raise Exception(send_exc)
                # Recipient-level failure — SMTP is fine, keep the
                # connection and move on to the next mail.
                return False

            with _lock:
                db.mark_sent(lead_id)
                sent += 1
                if (sent + failed) % 20 == 0:
                    db.update_campaign(campaign_id, sent=sent, failed=failed)
            pool.record_success(account)

            delay = worker.get_delay(email)
            if delay > 0:
                time.sleep(delay)
            return True

        def _worker_thread():
            """Each thread grabs an SMTP, connects, sends many mails."""
            nonlocal sent, failed
            while campaign_id in _runners and not mail_queue.empty():
                account = pool.acquire()
                if account is None:
                    if mail_queue.empty():
                        break
                    time.sleep(1)
                    continue

                try:
                    server_obj = pool.connect(account)
                except Exception as e:
                    logger.warning("Campaign %d: connect failed %s: %s", campaign_id, account.user, e)
                    # connect-time failure is transient: cooldown via pool
                    pool.suspend(account, "connect_failed")
                    if mail_queue.empty():
                        break
                    time.sleep(1)
                    continue

                warmup_wait = pool.get_warmup_delay(account)

                try:
                    while campaign_id in _runners:
                        # Block here while a freshness reset swaps the
                        # html_bodies / logo_variants under us. Returns
                        # immediately when the barrier is set (default).
                        freshness_barrier.wait(timeout=30)

                        try:
                            lead_id, email = mail_queue.get(timeout=2)
                        except queue.Empty:
                            break

                        if warmup_wait > 0:
                            time.sleep(warmup_wait)
                            warmup_wait = pool.get_warmup_delay(account)

                        try:
                            ok = _build_and_send(server_obj, account, lead_id, email)
                            # ok=False = recipient-level failure: SMTP is
                            # still usable, try the next mail with the
                            # same connection. (Old behaviour broke here,
                            # which forced a reconnect after every bounce.)
                        except Exception:
                            # SMTP-level failure raised inside _build_and_send.
                            # Pool was already notified via suspend/mark_dead.
                            break

                        time.sleep(cfg.get("normal_delay", 0.3))
                except Exception:
                    pass
                finally:
                    try:
                        server_obj.quit()
                    except Exception:
                        pass

        def _run_send_pass():
            """Fill queue from DB, run worker threads."""
            db.reset_in_progress(lead_list_id)
            while mail_queue.qsize() > 0:
                try:
                    mail_queue.get_nowait()
                except queue.Empty:
                    break

            leads = db._conn().execute(
                "SELECT id, email FROM trans_leads WHERE list_id=? AND state='PENDING' ORDER BY id",
                (lead_list_id,)).fetchall()
            if not leads:
                return

            for lead in leads:
                db._conn().execute("UPDATE trans_leads SET state='IN_PROGRESS' WHERE id=?", (lead["id"],))
                mail_queue.put((lead["id"], lead["email"]))
            db._conn().commit()

            threads = []
            for _ in range(thread_count):
                t = threading.Thread(target=_worker_thread, daemon=True)
                t.start()
                threads.append(t)

            for t in threads:
                t.join()

        try:
            _run_send_pass()

            # Blanket retry: every FAILED lead gets up to two more attempts.
            # No error-type filtering — bounce_log keeps classification for
            # stats, but we don't use it for flow decisions. A "permanent"
            # error from one SMTP is often transient on a second IP, and
            # truly dead mailboxes just fail again at near-zero cost.
            if cid in _runners and cfg.get("auto_retry_failed", True):
                retry_pauses = [5, 30]   # seconds before pass 2 / pass 3
                total_passes = len(retry_pauses) + 1
                for attempt, pause in enumerate(retry_pauses, start=2):
                    if cid not in _runners:
                        break
                    db.reset_in_progress(lead_list_id)
                    db._conn().execute(
                        "UPDATE trans_leads SET state='PENDING' "
                        "WHERE list_id=? AND state='FAILED'",
                        (lead_list_id,))
                    db._conn().commit()
                    pending = db._conn().execute(
                        "SELECT COUNT(*) FROM trans_leads "
                        "WHERE list_id=? AND state='PENDING'",
                        (lead_list_id,)).fetchone()[0]
                    if pending == 0:
                        break
                    logger.info(
                        "Campaign %d: retry pass %d/%d on %d failed leads, "
                        "pausing %ds first", cid, attempt, total_passes,
                        pending, pause)
                    time.sleep(pause)
                    _run_send_pass()
        finally:
            db.reset_in_progress(lead_list_id)

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
