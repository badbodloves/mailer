"""SMTP Management — add, import, edit, test, bulk check, delete."""
import time
import threading
import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("trans.smtps")
router = APIRouter()

_check_results = {}
_check_running = False


def _parse_proxy(proxy_str: str):
    if not proxy_str or not proxy_str.strip():
        return None
    p = proxy_str.strip().replace("socks5://", "").replace("socks://", "")
    user, pwd = "", ""
    if "@" in p:
        auth, p = p.rsplit("@", 1)
        if ":" in auth:
            user, pwd = auth.split(":", 1)
    parts = p.split(":")
    if len(parts) >= 4:
        return parts[0], int(parts[1]), parts[2], parts[3]
    if len(parts) >= 2:
        return parts[0], int(parts[1]), user, pwd
    return None


def _connect_smtp(host, port, username, password, proxy_str="", timeout=15):
    """Connect + login. Returns (server, error_str, error_type).
    error_type: None=ok, 'auth', 'timeout', 'connection', 'other'"""
    try:
        import socks as _socks
        HAS_SOCKS = True
    except ImportError:
        HAS_SOCKS = False

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("DEFAULT:@SECLEVEL=1")

    proxy = _parse_proxy(proxy_str)
    proxy_sock = None
    if proxy and HAS_SOCKS:
        try:
            ph, pp, pu, ppw = proxy
            proxy_sock = _socks.socksocket()
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
        return None, f"Auth failed: {e}", "auth"
    except (TimeoutError, OSError) as e:
        if "timed out" in str(e).lower():
            return None, f"Timeout: {e}", "timeout"
        return None, f"Connection: {e}", "connection"
    except Exception as e:
        return None, str(e), "other"


# ─── CRUD ──────────────────────────────────────────────

@router.get("/smtps", response_class=HTMLResponse)
async def smtps_page(request: Request):
    db = request.app.state.db
    smtps = [dict(s) for s in db.get_smtps()]
    return request.app.state.templates.TemplateResponse(request, "smtps.html", {
        "active": "smtps", "smtps": smtps, "db": db,
        "check_running": _check_running,
    })


@router.post("/smtps/add")
async def add_smtp(request: Request, host: str = Form(""), port: int = Form(587),
                   username: str = Form(""), password: str = Form(""),
                   proxy: str = Form(""), daily_limit: int = Form(0)):
    request.app.state.db.add_smtp(host.strip(), port, username.strip(),
                                    password.strip(), proxy.strip(), daily_limit)
    return RedirectResponse("/smtps", status_code=303)


@router.post("/smtps/import", response_class=HTMLResponse)
async def import_smtps(request: Request, smtp_text: str = Form("")):
    added = request.app.state.db.import_smtps(smtp_text)
    return HTMLResponse(f'<div class="alert alert-success">{added} SMTPs imported</div>')


@router.post("/smtps/{sid}/save")
async def save_smtp(request: Request, sid: int, host: str = Form(""), port: int = Form(587),
                    username: str = Form(""), password: str = Form(""),
                    proxy: str = Form(""), daily_limit: int = Form(0)):
    kw = {"host": host.strip(), "port": port, "username": username.strip(),
          "proxy": proxy.strip(), "daily_limit": daily_limit}
    if password.strip():
        kw["password"] = password.strip()
    request.app.state.db.update_smtp(sid, **kw)
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
    server, error, etype = _connect_smtp(row["host"], row["port"],
                                          row["username"], row["password"],
                                          row.get("proxy", ""))
    if server:
        server.quit()
        return HTMLResponse(f'<span style="color:var(--green)">&#10003; OK {row["host"]}:{row["port"]}</span>')
    return HTMLResponse(f'<span style="color:var(--red)">&#10007; {escape(error)}</span>')


# ─── Bulk Checker ──────────────────────────────────────

@router.post("/smtps/check-all", response_class=HTMLResponse)
async def start_bulk_check(request: Request,
                           proxy_override: str = Form(""),
                           send_test: int = Form(0),
                           test_to: str = Form(""),
                           test_subject: str = Form("SMTP Test"),
                           test_from_name: str = Form("Test"),
                           test_body: str = Form("SMTP connectivity test.")):
    global _check_running, _check_results
    if _check_running:
        return HTMLResponse('<div class="alert alert-warning">Check already running.</div>')

    db = request.app.state.db
    smtps = [dict(s) for s in db.get_smtps()]
    if not smtps:
        return HTMLResponse('<div class="alert alert-warning">No SMTPs to check.</div>')

    _check_results.clear()
    _check_running = True

    do_send = bool(send_test and test_to.strip())

    def worker():
        global _check_running
        try:
            for s in smtps:
                sid = s["id"]
                proxy = proxy_override.strip() or s.get("proxy", "")
                _check_results[sid] = {"status": "checking", "error": "",
                                        "error_type": None, "username": s["username"],
                                        "host": s["host"]}

                server, error, etype = _connect_smtp(
                    s["host"], s["port"], s["username"], s["password"], proxy, 20)

                if server:
                    if do_send:
                        try:
                            msg = MIMEText(test_body.strip() or "Test.", "plain", "utf-8")
                            msg["From"] = f'{test_from_name.strip()} <{s["username"]}>'
                            msg["To"] = test_to.strip()
                            msg["Subject"] = test_subject.strip() or "Test"
                            server.send_message(msg)
                            _check_results[sid].update(status="valid_sent", error="", error_type=None)
                        except Exception as e:
                            _check_results[sid].update(status="valid_send_failed",
                                                        error=str(e)[:200], error_type="send")
                    else:
                        _check_results[sid].update(status="valid", error="", error_type=None)
                    try:
                        server.quit()
                    except Exception:
                        pass
                else:
                    _check_results[sid].update(status="invalid", error=error or "Unknown",
                                                error_type=etype)
                time.sleep(0.3)
        finally:
            _check_running = False

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return HTMLResponse('<div class="alert alert-info">Check started...</div>')


@router.get("/smtps/check-results", response_class=HTMLResponse)
async def check_results_endpoint(request: Request):
    if not _check_results:
        if _check_running:
            return HTMLResponse('<p style="color:var(--fg2)">Starting...</p>')
        return HTMLResponse('')

    total = len(_check_results)
    checking = sum(1 for r in _check_results.values() if r["status"] == "checking")
    valid = sum(1 for r in _check_results.values() if r["status"] in ("valid", "valid_sent"))
    send_fail = sum(1 for r in _check_results.values() if r["status"] == "valid_send_failed")
    invalid = sum(1 for r in _check_results.values() if r["status"] == "invalid")
    auth_errors = sum(1 for r in _check_results.values()
                      if r["status"] == "invalid" and r.get("error_type") == "auth")

    header = '<div style="margin-bottom:10px">'
    if _check_running:
        header += f'<span class="badge badge-running">Checking {total - checking}/{total}</span> '
    else:
        header += '<span class="badge badge-finished">Done</span> '
    header += (f'<span style="color:var(--green);font-weight:600">{valid} valid</span> · '
               f'<span style="color:var(--red);font-weight:600">{invalid} invalid</span>')
    if send_fail:
        header += f' · <span style="color:var(--yellow)">{send_fail} send failed</span>'
    if auth_errors:
        header += f' · <span style="color:var(--red)">{auth_errors} auth</span>'
    header += '</div>'

    rows = ""
    for sid, r in _check_results.items():
        if r["status"] == "checking":
            badge = '<span class="badge badge-draft">...</span>'
        elif r["status"] in ("valid", "valid_sent"):
            extra = " + sent" if r["status"] == "valid_sent" else ""
            badge = f'<span class="badge badge-running">Valid{extra}</span>'
        elif r["status"] == "valid_send_failed":
            badge = '<span class="badge badge-paused">Login OK</span>'
        else:
            badge = f'<span class="badge badge-failed">{(r.get("error_type") or "error").upper()}</span>'

        err = f'<span style="font-size:11px;color:var(--red)">{escape(r.get("error","")[:80])}</span>' if r.get("error") else ""
        rows += (f'<tr><td style="font-family:monospace;font-size:12px">{escape(r["username"])}</td>'
                 f'<td style="font-size:12px">{escape(r["host"])}</td>'
                 f'<td>{badge}</td><td>{err}</td></tr>')

    html = header
    html += '<table><thead><tr><th>User</th><th>Host</th><th>Status</th><th>Error</th></tr></thead>'
    html += f'<tbody>{rows}</tbody></table>'

    if not _check_running and invalid > 0:
        html += '<div style="margin-top:14px" class="btn-group">'
        if auth_errors:
            html += (f'<button class="btn btn-danger btn-sm" '
                     f'hx-post="/smtps/cleanup?mode=auth" hx-target="#check-results" '
                     f'hx-swap="innerHTML" hx-confirm="Delete {auth_errors} auth failures?">'
                     f'Delete Auth Failures ({auth_errors})</button>')
        html += (f'<button class="btn btn-danger btn-sm" '
                 f'hx-post="/smtps/cleanup?mode=all" hx-target="#check-results" '
                 f'hx-swap="innerHTML" hx-confirm="Delete ALL {invalid} invalid?">'
                 f'Delete All Invalid ({invalid})</button>')
        html += '</div>'

    return HTMLResponse(html)


@router.post("/smtps/cleanup", response_class=HTMLResponse)
async def cleanup_smtps(request: Request, mode: str = "auth"):
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
    return HTMLResponse(
        f'<div class="alert alert-success">{deleted} deleted. '
        f'<a href="/smtps" style="color:var(--accent)">Reload page</a></div>')
