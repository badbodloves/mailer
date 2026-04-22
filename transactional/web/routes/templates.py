"""HTML Templates — CRUD + preview."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


@router.get("/templates", response_class=HTMLResponse)
async def templates_page(request: Request):
    db = request.app.state.db
    templates = [dict(t) for t in db.get_templates()]
    return request.app.state.templates.TemplateResponse(request, "trans_templates.html", {
        "active": "templates", "templates": templates, "db": db,
    })


@router.post("/templates/add")
async def add_template(request: Request, name: str = Form(""),
                       html_content: str = Form("")):
    if name.strip():
        request.app.state.db.add_template(name.strip(), html_content)
    return RedirectResponse("/templates", status_code=303)


@router.post("/templates/{tid}/save")
async def save_template(request: Request, tid: int, name: str = Form(""),
                        html_content: str = Form("")):
    request.app.state.db.update_template(tid, name.strip(), html_content)
    return RedirectResponse("/templates", status_code=303)


@router.post("/templates/{tid}/delete")
async def delete_template(request: Request, tid: int):
    request.app.state.db.delete_template(tid)
    return RedirectResponse("/templates", status_code=303)


@router.post("/templates/{tid}/preview", response_class=HTMLResponse)
async def preview_template(request: Request, tid: int):
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM trans_templates WHERE id=?", (tid,)).fetchone()
    if not row:
        return HTMLResponse("<p>Not found</p>")
    html = row["html_content"]
    html = html.replace("{email}", "preview@example.com")
    html = html.replace("{email_user}", "preview")
    html = html.replace("{domain}", "example.com")
    from html import escape
    return HTMLResponse(
        f'<iframe srcdoc="{escape(html)}" style="width:100%;height:400px;border:1px solid var(--border);border-radius:var(--radius);background:#fff"></iframe>'
    )
