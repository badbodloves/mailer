"""SMTP Management — add, import, edit, test, delete."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


@router.get("/smtps", response_class=HTMLResponse)
async def smtps_page(request: Request):
    db = request.app.state.db
    smtps = [dict(s) for s in db.get_smtps()]
    return request.app.state.templates.TemplateResponse(request, "smtps.html", {
        "active": "smtps", "smtps": smtps, "db": db,
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
    db = request.app.state.db
    added = db.import_smtps(smtp_text)
    from html import escape
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
    import smtplib, ssl
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if row["port"] == 465:
            server = smtplib.SMTP_SSL(row["host"], row["port"], timeout=15, context=ctx)
        else:
            server = smtplib.SMTP(row["host"], row["port"], timeout=15)
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls(context=ctx)
                server.ehlo()
        server.login(row["username"], row["password"])
        server.quit()
        return HTMLResponse(f'<span style="color:var(--green)">&#10003; Connected to {row["host"]}:{row["port"]}</span>')
    except Exception as e:
        return HTMLResponse(f'<span style="color:var(--red)">&#10007; {e}</span>')
