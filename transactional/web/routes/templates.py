"""HTML Templates — CRUD + bulk upload + preview + advanced previews."""
import os
import re
from html import escape
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List as TList

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


@router.post("/templates/bulk-upload")
async def bulk_upload_templates(request: Request,
                                files: TList[UploadFile] = File(...)):
    """Upload multiple HTML files — each becomes a template, named by filename."""
    db = request.app.state.db
    added = 0
    for f in files:
        if not f.filename:
            continue
        content = (await f.read()).decode("utf-8", errors="replace")
        if not content.strip():
            continue
        name = os.path.splitext(f.filename)[0]
        db.add_template(name, content)
        added += 1
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
    return HTMLResponse(
        f'<iframe srcdoc="{escape(html)}" style="width:100%;height:400px;border:1px solid var(--border);border-radius:var(--radius);background:#fff"></iframe>'
    )


def _get_template_html(db, tid: int):
    """Fetch template HTML content by id. Returns (html, error_response)."""
    row = db._conn().execute("SELECT * FROM trans_templates WHERE id=?", (tid,)).fetchone()
    if not row:
        return None, HTMLResponse("<p>Not found</p>")
    html = row["html_content"] or ""
    if not html.strip():
        return None, HTMLResponse("<p>Template is empty</p>")
    return html, None


def _resolve_variables(html: str) -> str:
    """Replace template variables with preview values."""
    html = html.replace("{email}", "preview@example.com")
    html = html.replace("{email_user}", "preview")
    html = html.replace("{domain}", "example.com")
    return html


@router.post("/templates/{tid}/preview-raw", response_class=HTMLResponse)
async def preview_raw(request: Request, tid: int):
    db = request.app.state.db
    html, err = _get_template_html(db, tid)
    if err:
        return err
    return HTMLResponse(
        f'<pre style="white-space:pre-wrap;word-break:break-all;max-height:500px;'
        f'overflow:auto;background:var(--bg2);padding:12px;border-radius:var(--radius);'
        f'font-size:12px;font-family:monospace">{escape(html)}</pre>'
    )


@router.post("/templates/{tid}/preview-processed", response_class=HTMLResponse)
async def preview_processed(request: Request, tid: int):
    db = request.app.state.db
    html, err = _get_template_html(db, tid)
    if err:
        return err
    html = _resolve_variables(html)
    return HTMLResponse(
        f'<iframe srcdoc="{escape(html)}" style="width:100%;height:400px;'
        f'border:1px solid var(--border);border-radius:var(--radius);background:#fff"></iframe>'
    )


@router.post("/templates/{tid}/preview-af", response_class=HTMLResponse)
async def preview_antifingerprint(request: Request, tid: int):
    db = request.app.state.db
    html, err = _get_template_html(db, tid)
    if err:
        return err
    html = _resolve_variables(html)
    try:
        from mailer.advanced_antifingerprint import AdvancedAntiFingerprintEngine
        afp = AdvancedAntiFingerprintEngine(enable_classes=True, structure_variation=0.5)
        html = afp.transform(html)
    except Exception as e:
        return HTMLResponse(
            f'<div class="alert alert-danger">Antifingerprint error: {escape(str(e))}</div>'
        )
    return HTMLResponse(
        f'<iframe srcdoc="{escape(html)}" style="width:100%;height:400px;'
        f'border:1px solid var(--border);border-radius:var(--radius);background:#fff"></iframe>'
        f'<details style="margin-top:8px"><summary style="cursor:pointer;font-size:12px;'
        f'color:var(--fg2)">View transformed source</summary>'
        f'<pre style="white-space:pre-wrap;word-break:break-all;max-height:300px;'
        f'overflow:auto;background:var(--bg2);padding:12px;border-radius:var(--radius);'
        f'font-size:11px;font-family:monospace">{escape(html)}</pre></details>'
    )


@router.post("/templates/{tid}/preview-mime", response_class=HTMLResponse)
async def preview_mime(request: Request, tid: int):
    db = request.app.state.db
    html, err = _get_template_html(db, tid)
    if err:
        return err
    html = _resolve_variables(html)
    plain_body = re.sub(r"<br\s*/?>", "\n", html)
    plain_body = re.sub(r"<[^>]+>", "", plain_body).strip()
    try:
        from mailer.mime_builder import MIMEBuilder
        raw_msg = MIMEBuilder.build_email(
            from_name="Preview User",
            from_email="preview@example.com",
            to_email="recipient@example.com",
            subject="Preview Subject",
            html_body=html,
            plain_body=plain_body,
        )
    except Exception as e:
        return HTMLResponse(
            f'<div class="alert alert-danger">MIME build error: {escape(str(e))}</div>'
        )
    truncated = raw_msg[:5000]
    if len(raw_msg) > 5000:
        truncated += f"\n\n... ({len(raw_msg):,} bytes total, truncated) ..."
    return HTMLResponse(
        f'<pre style="white-space:pre-wrap;word-break:break-all;max-height:500px;'
        f'overflow:auto;background:var(--bg2);padding:12px;border-radius:var(--radius);'
        f'font-size:11px;font-family:monospace">{escape(truncated)}</pre>'
        f'<p style="margin-top:6px;font-size:12px;color:var(--fg2)">'
        f'Total MIME size: {len(raw_msg):,} bytes</p>'
    )


@router.post("/templates/{tid}/text-ratio", response_class=HTMLResponse)
async def text_ratio(request: Request, tid: int):
    db = request.app.state.db
    html, err = _get_template_html(db, tid)
    if err:
        return err

    total_len = len(html)
    text_only = re.sub(r"<[^>]+>", "", html)
    text_only = re.sub(r"\s+", " ", text_only).strip()
    text_len = len(text_only)

    img_count = len(re.findall(r"<img\b", html, re.IGNORECASE))

    style_blocks = re.findall(r"<style[^>]*>.*?</style>", html, re.IGNORECASE | re.DOTALL)
    style_len = sum(len(s) for s in style_blocks)

    tag_content = re.findall(r"<[^>]+>", html)
    tag_len = sum(len(t) for t in tag_content)

    ratio = (text_len / total_len * 100) if total_len > 0 else 0
    color = "var(--green)" if ratio >= 30 else ("var(--yellow)" if ratio >= 15 else "var(--red)")
    rating = "Good" if ratio >= 30 else ("Fair" if ratio >= 15 else "Low")

    return HTMLResponse(
        f'<div style="padding:12px">'
        f'<div style="font-size:24px;font-weight:700;color:{color}">{ratio:.1f}%</div>'
        f'<div style="font-size:12px;color:var(--fg2);margin-bottom:12px">Text-to-HTML ratio ({rating})</div>'
        f'<table style="font-size:13px">'
        f'<tr><td style="padding:2px 12px 2px 0">Total HTML</td><td style="font-weight:600">{total_len:,} chars</td></tr>'
        f'<tr><td style="padding:2px 12px 2px 0">Visible text</td><td style="font-weight:600">{text_len:,} chars</td></tr>'
        f'<tr><td style="padding:2px 12px 2px 0">HTML tags</td><td style="font-weight:600">{tag_len:,} chars</td></tr>'
        f'<tr><td style="padding:2px 12px 2px 0">CSS styles</td><td style="font-weight:600">{style_len:,} chars</td></tr>'
        f'<tr><td style="padding:2px 12px 2px 0">Images</td><td style="font-weight:600">{img_count}</td></tr>'
        f'</table></div>'
    )
