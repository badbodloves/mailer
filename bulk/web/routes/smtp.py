"""SMTP Presets page — CRUD + connection testing."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


@router.get("/smtp", response_class=HTMLResponse)
async def smtp_page(request: Request):
    db = request.app.state.db
    db.reset_daily_counts()
    smtps = [dict(s) for s in db.get_smtps()]
    return request.app.state.templates.TemplateResponse(request, "smtp.html",
        {"active": "smtp", "smtps": smtps, "db": db})


@router.post("/smtp/add")
async def add_smtp(request: Request, name: str = Form(""), host: str = Form(""),
                    port: int = Form(587), username: str = Form(""),
                    password: str = Form(""), provider_type: str = Form("generic"),
                    daily_limit: int = Form(0), proxy: str = Form(""),
                    proxy_required: int = Form(0)):
    request.app.state.db.add_smtp(name, host, port, username, password,
                                   provider_type, daily_limit, proxy, proxy_required)
    return RedirectResponse("/smtp", status_code=303)


@router.post("/smtp/{sid}/delete")
async def delete_smtp(request: Request, sid: int):
    request.app.state.db.delete_smtp(sid)
    return RedirectResponse("/smtp", status_code=303)


@router.post("/smtp/{sid}/test", response_class=HTMLResponse)
async def test_smtp(request: Request, sid: int):
    """Test SMTP connection (with proxy if configured)."""
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM smtp_presets WHERE id=?", (sid,)).fetchone()
    if not row:
        return HTMLResponse('<span style="color:var(--red)">SMTP not found</span>')

    row = dict(row)
    from bulk.mailer.smtp_client import SMTPClient
    client = SMTPClient(
        row["host"], row["port"], row["username"], row["password"],
        proxy=row.get("proxy", ""),
        proxy_required=bool(row.get("proxy_required", 0)),
    )

    results = []

    if client.has_proxy:
        ok, msg = client.test_proxy()
        color = "var(--green)" if ok else "var(--red)"
        icon = "&#10003;" if ok else "&#10007;"
        results.append(f'<div style="color:{color};font-size:13px">{icon} Proxy: {msg}</div>')

    ok, msg = client.test_connection()
    color = "var(--green)" if ok else "var(--red)"
    icon = "&#10003;" if ok else "&#10007;"
    results.append(f'<div style="color:{color};font-size:13px">{icon} SMTP: {msg}</div>')

    return HTMLResponse("".join(results))
