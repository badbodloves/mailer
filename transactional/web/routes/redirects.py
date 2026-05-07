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
_s3_progress = {"running": False, "total": 0, "done": 0, "ok": 0, "errors": 0,
                "bucket": "", "stage": ""}


@router.get("/redirects", response_class=HTMLResponse)
async def redirects_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    redirects = [dict(r) for r in db.get_redirects(uid)]
    count = db.get_redirect_count(uid)
    redirect_pools = [dict(p, count=db.get_redirect_pool_count(p["id"])) for p in db.get_redirect_pools(uid)]
    cfg = db.get_config()
    s3_configured = bool(cfg.get("aws_access_key") and cfg.get("aws_secret_key"))
    return request.app.state.templates.TemplateResponse(request, "redirects.html", {
        "active": "redirects", "redirects": redirects, "db": db,
        "redirect_count": count, "redirect_pools": redirect_pools,
        "gen_progress": _gen_progress, "s3_progress": _s3_progress,
        "s3_configured": s3_configured,
    })


@router.post("/redirects/add-pool")
async def add_redirect_pool(request: Request, name: str = Form("")):
    if name.strip():
        uid = request.state.user["id"]
        request.app.state.db.add_redirect_pool(name.strip(), uid)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/pool/{pid}/delete")
async def delete_redirect_pool(request: Request, pid: int):
    request.app.state.db.delete_redirect_pool(pid)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/generate", response_class=HTMLResponse)
async def generate_redirects(request: Request,
                              target_url: str = Form(""),
                              count: int = Form(100),
                              gen_threads: int = Form(3),
                              pool_id: int = Form(0)):
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
                            db.add_redirect(url, target, gen_uid, pool_id)
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


@router.post("/redirects/generate-s3", response_class=HTMLResponse)
async def generate_s3_redirects(request: Request,
                                 target_url: str = Form(""),
                                 count: int = Form(50),
                                 tag: str = Form(""),
                                 pool_id: int = Form(0)):
    target = target_url.strip()
    if not target:
        return HTMLResponse('<div class="alert alert-warning">Enter a target URL.</div>')
    if not (target.startswith("http://") or target.startswith("https://")):
        return HTMLResponse('<div class="alert alert-warning">Target URL must start with http:// or https://</div>')
    if _s3_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">S3 generation already running.</div>')

    db = request.app.state.db
    cfg = db.get_config()
    access_key = cfg.get("aws_access_key", "").strip()
    secret_key = cfg.get("aws_secret_key", "").strip()
    region = cfg.get("aws_region", "eu-central-1").strip() or "eu-central-1"
    bucket_prefix = cfg.get("s3_bucket_prefix", "lk").strip() or "lk"

    if not access_key or not secret_key:
        return HTMLResponse(
            '<div class="alert alert-warning">AWS credentials missing. '
            'Set them under <a href="/config" style="color:var(--accent)">Config</a>.</div>'
        )

    count = max(1, min(count, 5000))
    gen_uid = request.state.user["id"]

    _s3_progress.update(running=True, total=count, done=0, ok=0, errors=0,
                         bucket="", stage="creating bucket")

    def worker():
        try:
            from mailer.s3_redirect import generate_links, _new_bucket_name, make_s3_client, create_public_bucket

            bucket = _new_bucket_name(bucket_prefix, tag)
            _s3_progress["bucket"] = bucket
            s3 = make_s3_client(access_key, secret_key, region)

            for attempt in range(3):
                try:
                    create_public_bucket(s3, bucket, region)
                    break
                except s3.exceptions.BucketAlreadyOwnedByYou:
                    break
                except s3.exceptions.BucketAlreadyExists:
                    bucket = _new_bucket_name(bucket_prefix, tag)
                    _s3_progress["bucket"] = bucket
                except Exception as e:
                    if attempt == 2:
                        raise
                    logger.warning("Bucket creation retry %d: %s", attempt + 1, e)
                    bucket = _new_bucket_name(bucket_prefix, tag)
                    _s3_progress["bucket"] = bucket

            _s3_progress["stage"] = "uploading"

            from mailer.s3_redirect import _redirect_html, _random_suffix
            body = _redirect_html(target).encode("utf-8")
            ok = 0
            errors = 0
            for i in range(count):
                key = f"{_random_suffix(10)}.html"
                try:
                    s3.put_object(
                        Bucket=bucket, Key=key, Body=body,
                        ContentType="text/html; charset=utf-8",
                        CacheControl="no-cache",
                    )
                    url = f"https://s3.{region}.amazonaws.com/{bucket}/{key}"
                    db.add_redirect(url, target, gen_uid, pool_id)
                    ok += 1
                except Exception as e:
                    errors += 1
                    logger.warning("S3 upload failed (%d/%d): %s", i + 1, count, e)
                _s3_progress["done"] = i + 1
                _s3_progress["ok"] = ok
                _s3_progress["errors"] = errors

            _s3_progress["stage"] = "done"
        except Exception as e:
            logger.error("S3 generation error: %s", e, exc_info=True)
            _s3_progress["errors"] += 1
            _s3_progress["stage"] = f"error: {e}"
        finally:
            _s3_progress["running"] = False

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return HTMLResponse(
        f'<div class="alert alert-info">Creating S3 bucket and uploading {count} redirect links...</div>'
        f'<div id="s3-progress" hx-get="/redirects/s3-status" '
        f'hx-trigger="every 2s" hx-swap="innerHTML"></div>'
    )


@router.get("/redirects/s3-status", response_class=HTMLResponse)
async def s3_gen_status(request: Request):
    p = _s3_progress
    bucket_html = ''
    if p["bucket"]:
        bucket_html = f'<div style="font-size:11px;color:var(--fg2);margin-bottom:6px">Bucket: <code>{escape(p["bucket"])}</code></div>'
    if p["running"]:
        done = p["done"]
        total = p["total"] or 1
        pct = int(done / total * 100)
        return HTMLResponse(
            f'{bucket_html}'
            f'<div style="font-size:12px;margin-bottom:4px">Stage: {escape(p["stage"])}</div>'
            f'<div class="progress" style="margin-bottom:8px">'
            f'<div class="progress-bar" style="width:{pct}%">{done}/{p["total"]}</div></div>'
            f'<p style="font-size:12px;color:var(--fg2)">'
            f'Uploaded: {p["ok"]} OK, {p["errors"]} errors</p>'
        )
    if p["stage"].startswith("error:"):
        return HTMLResponse(
            f'{bucket_html}'
            f'<div class="alert alert-danger">{escape(p["stage"])}</div>'
        )
    if p["ok"] > 0:
        return HTMLResponse(
            f'{bucket_html}'
            f'<div class="alert alert-success">Done! {p["ok"]} S3 redirect links generated. '
            f'<a href="/redirects" style="color:var(--accent)">Reload page</a></div>'
        )
    return HTMLResponse("")


@router.post("/redirects/add")
async def add_redirect(request: Request, short_url: str = Form(""),
                       pool_id: int = Form(0)):
    url = short_url.strip()
    if url:
        uid = request.state.user['id']
        request.app.state.db.add_redirect(url, '', uid, pool_id)
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
