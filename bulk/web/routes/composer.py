"""Composer — Message Templates CRUD."""
import json
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


@router.get("/composer", response_class=HTMLResponse)
async def composer_page(request: Request):
    db = request.app.state.db
    tpl = request.app.state.templates
    templates = []
    for t in db.get_templates():
        td = dict(t)
        td["html_files"] = json.loads(td.get("html_files_json") or "[]")
        td["sender_rotate"] = json.loads(td.get("sender_rotate_json") or "[]")
        td["settings"] = json.loads(td.get("settings_json") or "{}")
        templates.append(td)
    return tpl.TemplateResponse("composer.html", {
        "request": request, "active": "composer", "templates": templates, "db": db,
    })


@router.post("/composer/add")
async def add_template(request: Request,
                       name: str = Form(""),
                       subject_macro: str = Form(""),
                       html_files: str = Form(""),
                       html_rotate_every: int = Form(0),
                       pdf_path: str = Form(""),
                       pdf_macro_enabled: int = Form(0),
                       sender_rotate: str = Form(""),
                       sender_rotate_every: int = Form(0)):
    if not name.strip():
        return RedirectResponse("/composer", status_code=303)

    html_list = [f.strip() for f in html_files.splitlines() if f.strip()]
    sender_list = [s.strip() for s in sender_rotate.splitlines() if s.strip()]

    db = request.app.state.db
    db.add_template(
        name=name.strip(),
        subject_macro=subject_macro.strip(),
        html_files_json=json.dumps(html_list, ensure_ascii=False),
        html_rotate_every=html_rotate_every,
        pdf_path=pdf_path.strip(),
        pdf_macro_enabled=pdf_macro_enabled,
        sender_rotate_json=json.dumps(sender_list, ensure_ascii=False),
        sender_rotate_every=sender_rotate_every,
    )
    return RedirectResponse("/composer", status_code=303)


@router.post("/composer/{tid}/delete")
async def delete_template(request: Request, tid: int):
    request.app.state.db.delete_template(tid)
    return RedirectResponse("/composer", status_code=303)
