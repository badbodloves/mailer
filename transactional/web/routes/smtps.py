"""SMTP Lists — list-based management, bulk import (no proxy), checker."""
import time
import threading
import smtplib
import ssl
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()
_check_results = {}
_check_running = False


def _connect_smtp(host, port, username, password, proxy_str="", timeout=15):
    try:
        import socks as _socks
        HAS_SOCKS = True
    except ImportError:
        HAS_SOCKS = False

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("DEFAULT:@SECLEVEL=1")

    proxy_sock = None
    if proxy_str and HAS_SOCKS:
        p = proxy_str.strip().replace("socks5://", "").replace("socks://", "")
        user, pwd = "", ""
        if "@" in p:
            auth, p = p.rsplit("@", 1)
            if ":" in auth:
                user, pwd = auth.split(":", 1)
        parts = p.split(":")
        if len(parts) >= 2:
            try:
                proxy_sock = _socks.socksocket()
                ph, pp = parts[0], int(parts[1])
                pu = parts[2] if len(parts) > 2 else user
                ppw = parts[3] if len(parts) > 3 else pwd
                proxy_sock.set_proxy(_socks.SOCKS5, ph, pp,
                                     username=pu or None, password=ppw or None)
                proxy_sock.settimeout(timeout)
                proxy_sock.connect((host, port))
            except Exception as e:
                return None, f"Proxy: {e}", "connection"

    try:
        if port == 465:
            if proxy_sock:
                wrapped = ctx.wrap_socket(proxy_sock, server_hostname=host)
                server = smtplib.SMTP_SSL(context=ctx)
                server.sock = wrapped
                server._host = host
                server.file = server.sock.makefile("rb")
                server.getreply()
            else:
                server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
            server.ehlo()
        else:
            if proxy_sock:
                server = smtplib.SMTP()
                server.sock = proxy_sock
                server._host = host
                server.file = server.sock.makefile("rb")
                server.getreply()
            else:
                server = smtplib.SMTP(host, port, timeout=timeout)
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls(context=ctx)
                server.ehlo()
        server.login(username, password)
        return server, None, None
    except smtplib.SMTPAuthenticationError as e:
        return None, f"Auth: {e}", "auth"
    except (TimeoutError, OSError) as e:
        if "timed out" in str(e).lower():
            return None, f"Timeout: {e}", "timeout"
        return None, f"Connection: {e}", "connection"
    except Exception as e:
        return None, str(e), "other"


@router.get("/smtps", response_class=HTMLResponse)
async def smtps_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    smtp_lists = []
    for sl in db.get_smtp_lists(uid):
        sld = dict(sl)
        sld["count"] = db.get_smtp_count(sl["id"])
        smtp_lists.append(sld)
    return request.app.state.templates.TemplateResponse(request, "smtps.html", {
        "active": "smtps", "smtp_lists": smtp_lists, "db": db,
        "check_running": _check_running,
    })


@router.post("/smtps/create-list")
async def create_list(request: Request, name: str = Form("")):
    if name.strip():
        uid = request.state.user['id']
        request.app.state.db.create_smtp_list(name.strip(), uid)
    return RedirectResponse("/smtps", status_code=303)


@router.post("/smtps/{lid}/import", response_class=HTMLResponse)
async def import_smtps(request: Request, lid: int, smtp_text: str = Form("")):
    added = request.app.state.db.import_smtps(lid, smtp_text)
    return HTMLResponse(f'<div class="alert alert-success">{added} SMTPs imported (host,port,user,pass). '
                        f'<a href="/smtps" style="color:var(--accent)">Reload</a></div>')


@router.post("/smtps/list/{lid}/delete")
async def delete_list(request: Request, lid: int):
    request.app.state.db.delete_smtp_list(lid)
    return RedirectResponse("/smtps", status_code=303)


@router.post("/smtps/{sid}/delete")
async def delete_smtp(request: Request, sid: int):
    request.app.state.db.delete_smtp(sid)
    return RedirectResponse("/smtps", status_code=303)


@router.post("/smtps/{sid}/test", response_class=HTMLResponse)
async def test_smtp(request: Request, sid: int):
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM trans_smtps WHERE id=?", (sid,)).fetchone()
    if not row:
        return HTMLResponse('<span style="color:var(--red)">Not found</span>')
    row = dict(row)
    cfg = db.get_config()
    proxy = cfg.get("proxy_value", "").split("\n")[0].strip() if cfg.get("proxy_mode") != "off" else ""
    server, error, _ = _connect_smtp(row["host"], row["port"], row["username"], row["password"], proxy)
    if server:
        server.quit()
        return HTMLResponse(f'<span style="color:var(--green)">&#10003; OK</span>')
    return HTMLResponse(f'<span style="color:var(--red)">&#10007; {escape(error[:80])}</span>')


@router.post("/smtps/{lid}/check-all", response_class=HTMLResponse)
async def check_all(request: Request, lid: int,
                    check_threads: int = Form(5),
                    proxy_id: int = Form(0),
                    send_test: int = Form(0),
                    test_to: str = Form(""),
                    test_subject: str = Form("SMTP Test"),
                    test_from_name: str = Form("Test")):
    global _check_running, _check_results
    if _check_running:
        return HTMLResponse('<div class="alert alert-warning">Already running.</div>')

    db = request.app.state.db
    smtps = [dict(s) for s in db.get_smtps(lid)]
    if not smtps:
        return HTMLResponse('<div class="alert alert-warning">No SMTPs.</div>')

    proxy = ""
    if proxy_id:
        p = db.get_proxy(proxy_id)
        if p:
            proxy = dict(p)["value"].splitlines()[0].strip()
    if not proxy:
        cfg = db.get_config()
        pv = cfg.get("proxy_value", "")
        if pv.strip():
            proxy = pv.splitlines()[0].strip()

    do_send = bool(send_test and test_to.strip())

    _check_results.clear()
    _check_running = True
    num_threads = max(1, min(check_threads, 50))

    def check_one(s):
        _check_results[s["id"]] = {"status": "checking", "error": "", "error_type": None,
                                    "username": s["username"], "host": s["host"]}
        server, error, etype = _connect_smtp(s["host"], s["port"], s["username"], s["password"], proxy, 20)
        if server:
            if do_send:
                try:
                    from email.mime.text import MIMEText
                    msg = MIMEText("SMTP connectivity test.", "plain", "utf-8")
                    msg["From"] = f'{test_from_name} <{s["username"]}>'
                    msg["To"] = test_to.strip()
                    msg["Subject"] = test_subject
                    server.send_message(msg)
                    _check_results[s["id"]].update(status="valid_sent", error="", error_type=None)
                except Exception as e:
                    _check_results[s["id"]].update(status="valid_send_failed",
                                                    error=str(e)[:150], error_type="send")
            else:
                _check_results[s["id"]].update(status="valid", error="", error_type=None)
            try:
                server.quit()
            except Exception:
                pass
        else:
            _check_results[s["id"]].update(status="invalid", error=error or "Unknown", error_type=etype)

    def worker():
        global _check_running
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=num_threads) as ex:
                futures = {ex.submit(check_one, s): s["id"] for s in smtps}
                for f in as_completed(futures):
                    try:
                        f.result(timeout=30)
                    except Exception:
                        pass
        finally:
            _check_running = False

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(f'<div class="alert alert-info">Checking {len(smtps)} SMTPs ({num_threads} threads)...</div>')


@router.get("/smtps/check-results", response_class=HTMLResponse)
async def check_results(request: Request):
    if not _check_results:
        return HTMLResponse('<p style="color:var(--fg2)">Starting...</p>' if _check_running else '')

    valid = sum(1 for r in _check_results.values() if r["status"] in ("valid", "valid_sent"))
    invalid = sum(1 for r in _check_results.values() if r["status"] in ("invalid", "valid_send_failed"))
    auth_err = sum(1 for r in _check_results.values() if r.get("error_type") == "auth")
    checking = sum(1 for r in _check_results.values() if r["status"] == "checking")

    header = f'<div style="margin-bottom:8px">'
    header += f'<span class="badge badge-{"running" if _check_running else "finished"}">{"Checking..." if _check_running else "Done"}</span> '
    header += f'<span style="color:var(--green)">{valid} valid</span> · <span style="color:var(--red)">{invalid} invalid</span>'
    if auth_err:
        header += f' · <span style="color:var(--red)">{auth_err} auth</span>'
    header += '</div>'

    rows = ""
    for sid, r in _check_results.items():
        badge = {"checking": "draft", "valid": "running", "valid_sent": "running",
                 "valid_send_failed": "paused", "invalid": "failed"}.get(r["status"], "draft")
        label = r["status"].upper() if r["status"] != "checking" else "..."
        if r.get("error_type"):
            label = r["error_type"].upper()
        err = f'<span style="font-size:11px;color:var(--red)">{escape(r.get("error","")[:60])}</span>' if r.get("error") else ""
        rows += f'<tr><td style="font-family:monospace;font-size:12px">{escape(r["username"])}</td><td style="font-size:12px">{escape(r["host"])}</td><td><span class="badge badge-{badge}">{label}</span></td><td>{err}</td></tr>'

    html = header + f'<table><thead><tr><th>User</th><th>Host</th><th>Status</th><th>Error</th></tr></thead><tbody>{rows}</tbody></table>'

    if not _check_running and invalid > 0:
        html += '<div style="margin-top:12px" class="btn-group">'
        if auth_err:
            html += f'<button class="btn btn-danger btn-sm" hx-post="/smtps/cleanup?mode=auth" hx-target="#check-results" hx-swap="innerHTML" hx-confirm="Delete {auth_err} auth failures?">Delete Auth Failures ({auth_err})</button>'
        html += f'<button class="btn btn-danger btn-sm" hx-post="/smtps/cleanup?mode=all" hx-target="#check-results" hx-swap="innerHTML" hx-confirm="Delete ALL {invalid} invalid?">Delete All Invalid ({invalid})</button>'
        html += '</div>'
    return HTMLResponse(html)


@router.post("/smtps/cleanup", response_class=HTMLResponse)
async def cleanup(request: Request, mode: str = "auth"):
    db = request.app.state.db
    deleted = 0
    for sid, r in list(_check_results.items()):
        if r["status"] != "invalid":
            continue
        if mode == "auth" and r.get("error_type") != "auth":
            continue
        db.delete_smtp(sid)
        deleted += 1
        _check_results.pop(sid, None)
    return HTMLResponse(f'<div class="alert alert-success">{deleted} deleted. <a href="/smtps" style="color:var(--accent)">Reload</a></div>')
