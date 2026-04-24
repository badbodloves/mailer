"""Redirect Links — generate Google Share links, add, bulk-add, delete, clear."""
import threading
import time
import logging
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("trans.redirects")
router = APIRouter()

_gen_progress = {"running": False, "total": 0, "done": 0, "ok": 0, "errors": 0}


@router.get("/redirects", response_class=HTMLResponse)
async def redirects_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    redirects = [dict(r) for r in db.get_redirects(uid)]
    count = db.get_redirect_count(uid)
    return request.app.state.templates.TemplateResponse(request, "redirects.html", {
        "active": "redirects", "redirects": redirects, "db": db,
        "redirect_count": count, "gen_progress": _gen_progress,
    })


@router.post("/redirects/generate", response_class=HTMLResponse)
async def generate_redirects(request: Request,
                              target_url: str = Form(""),
                              count: int = Form(100),
                              gen_threads: int = Form(3)):
    target = target_url.strip()
    if not target:
        return HTMLResponse(
            '<div class="alert alert-warning">Enter a target URL.</div>'
        )
    if _gen_progress["running"]:
        return HTMLResponse(
            '<div class="alert alert-warning">Generation already running.</div>'
        )

    count = max(1, min(count, 5000))
    gen_threads = max(1, min(gen_threads, 10))
    db = request.app.state.db
    gen_uid = request.state.user["id"]

    _gen_progress.update(running=True, total=count, done=0, ok=0, errors=0)

    def worker():
        try:
            from mailer.redirect_manager import RedirectManager
            from concurrent.futures import ThreadPoolExecutor, as_completed

            generated = 0
            done = 0

            def gen_one(_):
                return RedirectManager._generate_one(target)

            with ThreadPoolExecutor(max_workers=gen_threads) as executor:
                futures = [executor.submit(gen_one, i) for i in range(count)]
                for f in as_completed(futures):
                    done += 1
                    _gen_progress["done"] = done
                    try:
                        url = f.result(timeout=15)
                        if url:
                            db.add_redirect(url, target, gen_uid)
                            generated += 1
                            _gen_progress["ok"] = generated
                        else:
                            _gen_progress["errors"] += 1
                    except Exception:
                        _gen_progress["errors"] += 1

            _gen_progress["done"] = count
            _gen_progress["ok"] = generated

        except Exception as e:
            logger.error("Redirect generation error: %s", e, exc_info=True)
            _gen_progress["errors"] += 1
        finally:
            _gen_progress["running"] = False

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return HTMLResponse(
        f'<div class="alert alert-info">Generating {count} redirect links '
        f'with {gen_threads} threads...</div>'
        f'<div id="gen-progress" hx-get="/redirects/status" '
        f'hx-trigger="every 2s" hx-swap="innerHTML"></div>'
    )


@router.post("/redirects/add")
async def add_redirect(request: Request, short_url: str = Form("")):
    url = short_url.strip()
    if url:
        uid = request.state.user['id']
        request.app.state.db.add_redirect(url, '', uid)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/bulk-add", response_class=HTMLResponse)
async def bulk_add(request: Request, urls: str = Form("")):
    db = request.app.state.db
    uid = request.state.user["id"]
    added = 0
    for line in urls.splitlines():
        url = line.strip()
        if url and (url.startswith("http://") or url.startswith("https://")):
            db.add_redirect(url, '', uid)
            added += 1
    if added:
        return HTMLResponse(
            f'<div class="alert alert-success">{added} redirect link(s) added. '
            f'<a href="/redirects" style="color:var(--accent)">Reload page</a></div>'
        )
    return HTMLResponse(
        '<div class="alert alert-warning">No valid URLs found. '
        'Each line must start with http:// or https://</div>'
    )


@router.post("/redirects/{rid}/delete")
async def delete_redirect(request: Request, rid: int):
    request.app.state.db.delete_redirect(rid)
    return RedirectResponse("/redirects", status_code=303)


@router.get("/redirects/export")
async def export_redirects(request: Request):
    """Export all redirect URLs as .txt download."""
    db = request.app.state.db
    uid = request.state.user['id']
    redirects = db.get_redirects(uid)
    lines = "\n".join(dict(r)["short_url"] for r in redirects)
    from fastapi.responses import Response
    return Response(content=lines, media_type="text/plain",
                    headers={"Content-Disposition": "attachment; filename=redirects.txt"})


@router.post("/redirects/clear")
async def clear_redirects(request: Request):
    uid = request.state.user['id']
    request.app.state.db.clear_redirects(uid)
    return RedirectResponse("/redirects", status_code=303)


@router.get("/redirects/status", response_class=HTMLResponse)
async def gen_status(request: Request):
    p = _gen_progress
    if p["running"]:
        done = p["done"]
        total = p["total"]
        pct = int(done / total * 100) if total > 0 else 0
        return HTMLResponse(
            f'<div class="progress" style="margin-bottom:8px">'
            f'<div class="progress-bar" style="width:{pct}%">{done}/{total}</div></div>'
            f'<p style="font-size:12px;color:var(--fg2)">'
            f'Generated: {p["ok"]} OK, {p["errors"]} errors</p>'
        )
    if p["ok"] > 0:
        return HTMLResponse(
            f'<div class="alert alert-success">Done! {p["ok"]} redirect links generated. '
            f'<a href="/redirects" style="color:var(--accent)">Reload page</a></div>'
        )
    if p["errors"] > 0:
        return HTMLResponse(
            '<div class="alert alert-danger">Generation failed. '
            'Check logs for details.</div>'
        )
    return HTMLResponse("")
