"""SMTP Presets page."""
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
                    daily_limit: int = Form(0), proxy: str = Form("")):
    request.app.state.db.add_smtp(name, host, port, username, password, provider_type, daily_limit, proxy)
    return RedirectResponse("/smtp", status_code=303)

@router.post("/smtp/{sid}/delete")
async def delete_smtp(request: Request, sid: int):
    request.app.state.db.delete_smtp(sid)
    return RedirectResponse("/smtp", status_code=303)
