"""Redirect Links — generate Google Share links, add, bulk-add, delete, clear."""
import random
import threading
import time
import logging
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("trans.redirects")
router = APIRouter()

AWS_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "af-south-1",
    "ap-east-1", "ap-south-1", "ap-south-2",
    "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-southeast-1", "ap-southeast-2", "ap-southeast-3", "ap-southeast-4",
    "ca-central-1", "ca-west-1",
    "eu-central-1", "eu-central-2",
    "eu-west-1", "eu-west-2", "eu-west-3",
    "eu-north-1", "eu-south-1", "eu-south-2",
    "me-south-1", "me-central-1",
    "sa-east-1",
    "il-central-1",
]

_gen_progress = {"running": False, "total": 0, "done": 0, "ok": 0, "errors": 0}
_s3_progress = {"running": False, "total": 0, "done": 0, "ok": 0, "errors": 0,
                "bucket": "", "stage": "", "region": ""}


@router.get("/redirects", response_class=HTMLResponse)
async def redirects_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    redirects = [dict(r) for r in db.get_redirects(uid)]
    count = db.get_redirect_count(uid)
    redirect_pools = [dict(p, count=db.get_redirect_pool_count(p["id"])) for p in db.get_redirect_pools(uid)]
    cfg = db.get_config()
    s3_configured = bool(cfg.get("aws_access_key") and cfg.get("aws_secret_key"))
    proxies = [dict(p) for p in db.get_proxies(uid)]
    return request.app.state.templates.TemplateResponse(request, "redirects.html", {
        "active": "redirects", "redirects": redirects, "db": db,
        "redirect_count": count, "redirect_pools": redirect_pools,
        "gen_progress": _gen_progress, "s3_progress": _s3_progress,
        "s3_configured": s3_configured, "cfg": cfg,
        "aws_regions": AWS_REGIONS,
        "proxies": proxies,
    })


def _resolve_proxy(db, cfg: dict) -> str:
    """Look up the AWS proxy value from trans_proxies via aws_proxy_id.
    Returns '' when none is selected or the entry no longer exists."""
    pid = int(cfg.get("aws_proxy_id", 0) or 0)
    if not pid:
        return ""
    row = db.get_proxy(pid)
    if not row:
        return ""
    return (dict(row).get("value") or "").strip()


@router.post("/redirects/append-ref-toggle")
async def toggle_append_ref(request: Request, append_ref: str = Form("")):
    db = request.app.state.db
    cfg = db.get_config()
    cfg["redirect_append_ref"] = bool(append_ref)
    db.save_config(cfg)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/s3-config")
async def save_s3_config(request: Request,
                          aws_access_key: str = Form(""),
                          aws_secret_key: str = Form(""),
                          s3_bucket_prefix: str = Form("lk"),
                          aws_proxy_id: int = Form(0)):
    db = request.app.state.db
    cfg = db.get_config()
    cfg["aws_access_key"] = aws_access_key.strip()
    cfg["aws_secret_key"] = aws_secret_key.strip()
    cfg["s3_bucket_prefix"] = s3_bucket_prefix.strip() or "lk"
    cfg["aws_proxy_id"] = int(aws_proxy_id or 0)
    db.save_config(cfg)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/s3-test", response_class=HTMLResponse)
async def test_s3_connection(request: Request):
    """Run a fast list_buckets to verify creds + proxy."""
    db = request.app.state.db
    cfg = db.get_config()
    access = cfg.get("aws_access_key", "").strip()
    secret = cfg.get("aws_secret_key", "").strip()
    region = cfg.get("aws_region", "eu-central-1").strip() or "eu-central-1"
    proxy = _resolve_proxy(db, cfg)
    if not access or not secret:
        return HTMLResponse('<div class="alert alert-warning">Missing credentials.</div>')
    try:
        from mailer.s3_redirect import make_s3_client
        s3 = make_s3_client(access, secret, region, proxy=proxy)
        resp = s3.list_buckets()
        n = len(resp.get("Buckets", []))
        proxy_str = f" via proxy <code>{escape(proxy)}</code>" if proxy else ""
        return HTMLResponse(
            f'<div class="alert alert-success">OK — {n} bucket(s) visible '
            f'(region {escape(region)}){proxy_str}.</div>'
        )
    except Exception as e:
        return HTMLResponse(
            f'<div class="alert alert-danger">Connection failed: {escape(str(e)[:300])}</div>'
        )


@router.get("/redirects/pool/{pid}/export")
async def export_pool(request: Request, pid: int):
    """Export one redirect pool's links as plain .txt."""
    from fastapi.responses import Response
    db = request.app.state.db
    links = [dict(r)["short_url"] for r in db.get_redirects_by_pool(pid)]
    pool_name = "pool"
    for p in db.get_redirect_pools(request.state.user["id"]):
        if dict(p)["id"] == pid:
            pool_name = "".join(c if c.isalnum() or c in "-_" else "_"
                                for c in dict(p)["name"]).strip("_") or f"pool{pid}"
            break
    return Response(
        content="\n".join(links),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename={pool_name}.txt'},
    )


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


GOOGLE_DOMAINS = [
    "google.ch", "google.fr", "google.nl", "google.be", "google.se",
    "google.dk", "google.fi", "google.hu", "google.no", "google.com",
]


def _rewrite_share_google(url: str) -> str:
    """Convert https://share.google/ID → https://www.google.XX/share.google?q=ID
    with a random domain from the pool."""
    prefix = "https://share.google/"
    if not url.startswith(prefix):
        return url
    link_id = url[len(prefix):].split("?")[0].split("#")[0]
    if not link_id:
        return url
    domain = random.choice(GOOGLE_DOMAINS)
    return f"https://www.{domain}/share.google?q={link_id}"


@router.post("/redirects/generate", response_class=HTMLResponse)
async def generate_redirects(request: Request,
                              target_url: str = Form(""),
                              count: int = Form(100),
                              gen_threads: int = Form(3),
                              pool_id: int = Form(0),
                              google_rewrite: str = Form("")):
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
    do_rewrite = bool(google_rewrite)

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
                            if do_rewrite:
                                url = _rewrite_share_google(url)
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
    fmt_label = " (google.xx format)" if do_rewrite else ""
    return HTMLResponse(
        f'<div class="alert alert-info">Generating {count} redirect links '
        f'with {gen_threads} threads{fmt_label}...</div>'
        f'<div id="gen-progress" hx-get="/redirects/status" '
        f'hx-trigger="every 2s" hx-swap="innerHTML"></div>'
    )


@router.post("/redirects/generate-s3", response_class=HTMLResponse)
async def generate_s3_redirects(request: Request,
                                 target_url: str = Form(""),
                                 count: int = Form(50),
                                 tag: str = Form(""),
                                 region: str = Form("random"),
                                 pool_id: int = Form(0),
                                 bot_filter: str = Form("")):
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
    bucket_prefix = cfg.get("s3_bucket_prefix", "lk").strip() or "lk"
    proxy = _resolve_proxy(db, cfg)

    if not access_key or not secret_key:
        return HTMLResponse(
            '<div class="alert alert-warning">AWS credentials missing. '
            'Save them in the S3 Settings box above.</div>'
        )

    region = (region or "").strip().lower()
    if region == "random" or not region:
        region = random.choice(AWS_REGIONS)
    elif region not in AWS_REGIONS:
        return HTMLResponse(f'<div class="alert alert-warning">Unknown region: {escape(region)}</div>')

    count = max(1, min(count, 5000))
    gen_uid = request.state.user["id"]

    use_bot_filter = bool(bot_filter)
    _s3_progress.update(running=True, total=count, done=0, ok=0, errors=0,
                         bucket="", stage="creating bucket", region=region)

    def worker():
        try:
            from mailer.s3_redirect import generate_links, _new_bucket_name, make_s3_client, create_public_bucket

            bucket = _new_bucket_name(bucket_prefix, tag)
            _s3_progress["bucket"] = bucket
            s3 = make_s3_client(access_key, secret_key, region, proxy=proxy)

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
            body = _redirect_html(target, bot_filter=use_bot_filter).encode("utf-8")
            ok = 0
            errors = 0
            for i in range(count):
                key = _random_suffix(10)
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
    if p["bucket"] or p.get("region"):
        region_str = f' <span style="margin-left:6px">Region: <code>{escape(p.get("region", ""))}</code></span>' if p.get("region") else ''
        bucket_str = f'Bucket: <code>{escape(p["bucket"])}</code>' if p["bucket"] else ''
        bucket_html = f'<div style="font-size:11px;color:var(--fg2);margin-bottom:6px">{bucket_str}{region_str}</div>'
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
