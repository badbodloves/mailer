"""Macros — spintax pools, file import, edit."""
import os
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List as TList

router = APIRouter()


@router.get("/macros", response_class=HTMLResponse)
async def macros_page(request: Request):
    db = request.app.state.db
    macros = [dict(m) for m in db.get_macros()]
    for m in macros:
        lines = [l for l in (m.get("values_text") or "").splitlines() if l.strip()]
        m["line_count"] = len(lines)
    return request.app.state.templates.TemplateResponse(request, "macros.html", {
        "active": "macros", "macros": macros, "db": db,
    })


@router.post("/macros/add")
async def add_macro(request: Request, name: str = Form(""),
                    values_text: str = Form(""), rotate_every: int = Form(0)):
    if name.strip():
        request.app.state.db.add_macro(name.strip(), values_text, rotate_every)
    return RedirectResponse("/macros", status_code=303)


@router.post("/macros/{mid}/save")
async def save_macro(request: Request, mid: int,
                     values_text: str = Form(""), rotate_every: int = Form(0)):
    request.app.state.db.update_macro(mid, values_text, rotate_every)
    return RedirectResponse("/macros", status_code=303)


@router.post("/macros/{mid}/delete")
async def delete_macro(request: Request, mid: int):
    request.app.state.db.delete_macro(mid)
    return RedirectResponse("/macros", status_code=303)


@router.post("/macros/import-files", response_class=HTMLResponse)
async def import_files(request: Request, files: TList[UploadFile] = File(...)):
    """Import .txt files as macros. Filename (without ext) = macro name."""
    db = request.app.state.db
    added = 0
    for f in files:
        if not f.filename:
            continue
        content = (await f.read()).decode("utf-8", errors="replace")
        name = os.path.splitext(f.filename)[0]
        db.add_macro(name, content.strip())
        added += 1
    from html import escape
    return HTMLResponse(
        f'<div class="alert alert-success">{added} macro(s) imported. '
        f'<a href="/macros" style="color:var(--accent)">Reload</a></div>')
