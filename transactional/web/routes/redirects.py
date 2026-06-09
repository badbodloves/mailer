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

# Subset used when picking a fresh region per-bucket. Restricted to
# regions with broad CDN propagation and zero opt-in friction — leaves
# out Brazil, Israel, Middle East, South Africa, opt-in APAC etc.
POPULAR_AWS_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-west-3",
    "eu-central-1", "eu-north-1",
    "ca-central-1",
    "ap-northeast-1", "ap-southeast-1", "ap-southeast-2",
]

_gen_progress = {"running": False, "total": 0, "done": 0, "ok": 0, "errors": 0}
_s3_progress = {"running": False, "total": 0, "done": 0, "ok": 0, "errors": 0,
                "bucket": "", "stage": "", "region": "",
                "last_error": "", "aborted": False}

CONSECUTIVE_ERROR_LIMIT = 5


@router.get("/redirects", response_class=HTMLResponse)
async def redirects_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    redirects = [dict(r) for r in db.get_redirects(uid)]
    count = db.get_redirect_count(uid)
    redirect_pools = [dict(p, count=db.get_redirect_pool_count(p["id"])) for p in db.get_redirect_pools(uid)]
    cfg = db.get_config()
    s3_accounts = [dict(a) for a in db.get_s3_accounts(uid)]
    s3_configured = bool(s3_accounts) or bool(cfg.get("aws_access_key") and cfg.get("aws_secret_key"))
    proxies = [dict(p) for p in db.get_proxies(uid)]
    return request.app.state.templates.TemplateResponse(request, "redirects.html", {
        "active": "redirects", "redirects": redirects, "db": db,
        "redirect_count": count, "redirect_pools": redirect_pools,
        "gen_progress": _gen_progress, "s3_progress": _s3_progress,
        "s3_configured": s3_configured, "cfg": cfg,
        "aws_regions": AWS_REGIONS,
        "proxies": proxies,
        "s3_accounts": s3_accounts,
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


def _resolve_proxy_by_id(db, proxy_id: int) -> str:
    if not proxy_id:
        return ""
    row = db.get_proxy(proxy_id)
    if not row:
        return ""
    return (dict(row).get("value") or "").strip()


def _resolve_s3_account(db, uid: int, account_id: int = 0,
                        region_override: str = "") -> dict:
    """Return {name, access_key, secret_key, region, bucket_prefix, proxy_val}.
    Picks the specified account or the primary if 0. Falls back to the
    legacy flat config if no accounts exist."""
    row = None
    if account_id:
        row = db.get_s3_account(account_id)
    if row is None:
        row = db.get_primary_s3_account(uid)
    if row is not None:
        r = dict(row)
        region = (region_override or r.get("region") or "eu-central-1").strip()
        return {
            "name": r.get("name", ""),
            "access_key": (r.get("access_key") or "").strip(),
            "secret_key": (r.get("secret_key") or "").strip(),
            "region": region or "eu-central-1",
            "bucket_prefix": (r.get("bucket_prefix") or "lk").strip() or "lk",
            "proxy_val": _resolve_proxy_by_id(db, r.get("proxy_id", 0) or 0),
        }
    # Legacy fall-through
    cfg = db.get_config()
    return {
        "name": "(legacy)",
        "access_key": (cfg.get("aws_access_key") or "").strip(),
        "secret_key": (cfg.get("aws_secret_key") or "").strip(),
        "region": (region_override or cfg.get("aws_region") or "eu-central-1").strip() or "eu-central-1",
        "bucket_prefix": (cfg.get("s3_bucket_prefix") or "lk").strip() or "lk",
        "proxy_val": _resolve_proxy(db, cfg),
    }


def _parse_s3_url(url: str) -> tuple:
    """Pull (region, bucket, key) out of a path-style S3 URL.
    Returns (None, None, None) when the URL doesn't look like one we
    generated."""
    import re
    m = re.match(r"^https://s3\.([a-z0-9-]+)\.amazonaws\.com/([a-z0-9.-]+)/(.+)$", url)
    if not m:
        return None, None, None
    return m.group(1), m.group(2), m.group(3)


@router.post("/redirects/append-ref-toggle")
async def toggle_append_ref(request: Request, append_ref: str = Form("")):
    db = request.app.state.db
    cfg = db.get_config()
    cfg["redirect_append_ref"] = bool(append_ref)
    db.save_config(cfg)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/s3-accounts/add")
async def add_s3_account(request: Request,
                          name: str = Form(""),
                          access_key: str = Form(""),
                          secret_key: str = Form(""),
                          region: str = Form("eu-central-1"),
                          bucket_prefix: str = Form("lk"),
                          proxy_id: int = Form(0)):
    if not (name.strip() and access_key.strip() and secret_key.strip()):
        return RedirectResponse("/redirects", status_code=303)
    db = request.app.state.db
    uid = request.state.user["id"]
    db.add_s3_account(name.strip(), access_key.strip(), secret_key.strip(),
                      region.strip() or "eu-central-1",
                      bucket_prefix.strip() or "lk",
                      int(proxy_id or 0), uid)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/s3-accounts/{aid}/update")
async def update_s3_account(request: Request, aid: int,
                             name: str = Form(""),
                             access_key: str = Form(""),
                             secret_key: str = Form(""),
                             region: str = Form("eu-central-1"),
                             bucket_prefix: str = Form("lk"),
                             proxy_id: int = Form(0)):
    fields = {}
    if name.strip():
        fields["name"] = name.strip()
    if access_key.strip():
        fields["access_key"] = access_key.strip()
    if secret_key.strip():
        fields["secret_key"] = secret_key.strip()
    if region.strip():
        fields["region"] = region.strip()
    fields["bucket_prefix"] = bucket_prefix.strip() or "lk"
    fields["proxy_id"] = int(proxy_id or 0)
    request.app.state.db.update_s3_account(aid, **fields)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/s3-accounts/{aid}/set-primary")
async def set_primary_s3_account(request: Request, aid: int):
    request.app.state.db.set_primary_s3_account(aid)
    return RedirectResponse("/redirects", status_code=303)


@router.post("/redirects/s3-accounts/{aid}/delete")
async def delete_s3_account(request: Request, aid: int):
    request.app.state.db.delete_s3_account(aid)
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
async def test_s3_connection(request: Request,
                              s3_account_id: int = Form(0)):
    """Run a fast list_buckets to verify creds + proxy for the chosen
    (or primary) S3 account."""
    db = request.app.state.db
    uid = request.state.user["id"]
    acc = _resolve_s3_account(db, uid, int(s3_account_id or 0))
    if not acc["access_key"] or not acc["secret_key"]:
        return HTMLResponse('<div class="alert alert-warning">Missing credentials for this account.</div>')
    try:
        from mailer.s3_redirect import make_s3_client
        s3 = make_s3_client(acc["access_key"], acc["secret_key"],
                            acc["region"], proxy=acc["proxy_val"])
        resp = s3.list_buckets()
        n = len(resp.get("Buckets", []))
        proxy_str = f" via proxy <code>{escape(acc['proxy_val'])}</code>" if acc["proxy_val"] else ""
        label = f" [{escape(acc['name'])}]" if acc.get("name") else ""
        return HTMLResponse(
            f'<div class="alert alert-success">OK{label} — {n} bucket(s) visible '
            f'(region {escape(acc["region"])}){proxy_str}.</div>'
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
                                 bot_filter: str = Form(""),
                                 unique_bucket: str = Form(""),
                                 s3_account_id: int = Form(0)):
    target = target_url.strip()
    if not target:
        return HTMLResponse('<div class="alert alert-warning">Enter a target URL.</div>')
    if not (target.startswith("http://") or target.startswith("https://")):
        return HTMLResponse('<div class="alert alert-warning">Target URL must start with http:// or https://</div>')
    if _s3_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">S3 generation already running.</div>')

    db = request.app.state.db
    uid = request.state.user["id"]
    region_override = (region or "").strip().lower()
    region_was_random = region_override in ("random", "")
    if region_was_random:
        chosen_region = random.choice(POPULAR_AWS_REGIONS)
    elif region_override not in AWS_REGIONS:
        return HTMLResponse(f'<div class="alert alert-warning">Unknown region: {escape(region_override)}</div>')
    else:
        chosen_region = region_override

    acc = _resolve_s3_account(db, uid, int(s3_account_id or 0),
                               region_override=chosen_region)
    access_key = acc["access_key"]
    secret_key = acc["secret_key"]
    bucket_prefix = acc["bucket_prefix"]
    proxy = acc["proxy_val"]
    region = acc["region"]
    account_label = acc.get("name", "")

    if not access_key or not secret_key:
        return HTMLResponse(
            '<div class="alert alert-warning">AWS credentials missing for the selected account. '
            'Add an S3 account in the S3 Accounts box below.</div>'
        )

    count = max(1, min(count, 5000))
    gen_uid = uid

    use_bot_filter = bool(bot_filter)
    per_link_bucket = bool(unique_bucket)
    # Each fresh bucket picks its own region from POPULAR_AWS_REGIONS
    # only when the user asked for "Random" — if they picked a specific
    # region we respect it across the whole batch.
    region_per_bucket = per_link_bucket and region_was_random
    _s3_progress.update(running=True, total=count, done=0, ok=0, errors=0,
                         bucket="", stage="creating bucket", region=region,
                         last_error="", aborted=False)
    logger.info("S3 gen: account=%s region=%s region_per_bucket=%s count=%d target=%s",
                account_label or "(primary)", region, region_per_bucket, count, target[:80])

    def worker():
        try:
            from mailer.s3_redirect import _new_bucket_name, make_s3_client, create_public_bucket
            from mailer.s3_redirect import _redirect_html, _random_suffix

            # Cache one S3 client per region. With region_per_bucket=False
            # this is just a single client; with True we lazily build one
            # for every region we hit, but no more than ~12 ever exist.
            s3_clients = {}
            def _client_for(r: str):
                if r not in s3_clients:
                    s3_clients[r] = make_s3_client(
                        access_key, secret_key, r, proxy=proxy)
                return s3_clients[r]

            body = _redirect_html(target, bot_filter=use_bot_filter).encode("utf-8")

            def _spawn_bucket(r: str):
                """Create one fresh bucket in region r, return its name."""
                cli = _client_for(r)
                b = _new_bucket_name(bucket_prefix, tag)
                for attempt in range(3):
                    try:
                        create_public_bucket(cli, b, r)
                        return b
                    except cli.exceptions.BucketAlreadyOwnedByYou:
                        return b
                    except cli.exceptions.BucketAlreadyExists:
                        b = _new_bucket_name(bucket_prefix, tag)
                    except Exception as e:
                        if attempt == 2:
                            raise
                        logger.warning("Bucket creation retry %d: %s", attempt + 1, e)
                        b = _new_bucket_name(bucket_prefix, tag)
                return b

            shared_bucket = None
            if not per_link_bucket:
                shared_bucket = _spawn_bucket(region)
                _s3_progress["bucket"] = shared_bucket

            _s3_progress["stage"] = "uploading" if not per_link_bucket else "creating buckets + uploading"

            ok = 0
            errors = 0
            consecutive = 0
            for i in range(count):
                if consecutive >= CONSECUTIVE_ERROR_LIMIT:
                    _s3_progress["aborted"] = True
                    _s3_progress["stage"] = (
                        f"aborted after {consecutive} consecutive errors. "
                        f"Last: {_s3_progress.get('last_error', '')[:200]}"
                    )
                    logger.error(
                        "S3 gen aborted at %d/%d after %d consecutive errors",
                        i, count, consecutive,
                    )
                    break

                if per_link_bucket:
                    this_region = (
                        random.choice(POPULAR_AWS_REGIONS)
                        if region_per_bucket else region
                    )
                    _s3_progress["region"] = this_region
                    _s3_progress["stage"] = (
                        f"creating bucket {i + 1}/{count} in {this_region}"
                    )
                    try:
                        bucket = _spawn_bucket(this_region)
                    except Exception as e:
                        errors += 1
                        consecutive += 1
                        _s3_progress["last_error"] = (
                            f"bucket {i+1} ({this_region}): {str(e)[:300]}"
                        )
                        logger.warning("Per-link bucket %d in %s failed: %s",
                                        i + 1, this_region, e)
                        _s3_progress["done"] = i + 1
                        _s3_progress["errors"] = errors
                        continue
                    _s3_progress["bucket"] = bucket
                    _s3_progress["stage"] = (
                        f"uploading object {i + 1}/{count} ({this_region})"
                    )
                else:
                    bucket = shared_bucket
                    this_region = region
                key = _random_suffix(10)
                try:
                    cli = _client_for(this_region)
                    cli.put_object(
                        Bucket=bucket, Key=key, Body=body,
                        ContentType="text/html; charset=utf-8",
                        CacheControl="no-cache",
                    )
                    url = f"https://s3.{this_region}.amazonaws.com/{bucket}/{key}"
                    db.add_redirect(url, target, gen_uid, pool_id)
                    ok += 1
                    consecutive = 0   # success resets the streak
                except Exception as e:
                    errors += 1
                    consecutive += 1
                    _s3_progress["last_error"] = f"upload {i+1}: {str(e)[:300]}"
                    logger.warning("S3 upload failed (%d/%d): %s", i + 1, count, e)
                _s3_progress["done"] = i + 1
                _s3_progress["ok"] = ok
                _s3_progress["errors"] = errors

            if not _s3_progress["aborted"]:
                _s3_progress["stage"] = "done"
        except Exception as e:
            logger.error("S3 generation error: %s", e, exc_info=True)
            _s3_progress["errors"] += 1
            _s3_progress["aborted"] = True
            _s3_progress["last_error"] = str(e)[:400]
            _s3_progress["stage"] = f"error: {str(e)[:200]}"
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

    last_err_html = ''
    if p.get("last_error"):
        last_err_html = (
            f'<div style="margin-top:6px;padding:6px;border:1px solid var(--red);'
            f'border-radius:var(--radius);background:#ffeded;color:#a00;'
            f'font-family:monospace;font-size:11px;white-space:pre-wrap;max-height:120px;overflow:auto">'
            f'<strong>Last error:</strong> {escape(p["last_error"])}</div>'
        )

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
            f'{last_err_html}'
        )

    if p.get("aborted"):
        return HTMLResponse(
            f'{bucket_html}'
            f'<div class="alert alert-danger">'
            f'Aborted: {escape(p["stage"])}<br>'
            f'<strong>{p["ok"]} OK</strong>, {p["errors"]} errors before stopping.'
            f'</div>{last_err_html}'
        )
    if p["stage"].startswith("error:"):
        return HTMLResponse(
            f'{bucket_html}'
            f'<div class="alert alert-danger">{escape(p["stage"])}</div>'
            f'{last_err_html}'
        )
    if p["ok"] > 0:
        return HTMLResponse(
            f'{bucket_html}'
            f'<div class="alert alert-success">Done! {p["ok"]} S3 redirect links generated. '
            f'<a href="/redirects" style="color:var(--accent)">Reload page</a></div>'
            f'{last_err_html if p["errors"] > 0 else ""}'
        )
    return HTMLResponse("")


def _re_upload_redirect(db, cfg, link_row: dict, new_target: str,
                         bot_filter: bool) -> tuple:
    """Re-PUT the HTML for one stored redirect link with a new destination.
    Returns (ok: bool, error_msg: str)."""
    from mailer.s3_redirect import make_s3_client, _redirect_html
    region, bucket, key = _parse_s3_url(link_row.get("short_url", ""))
    if not bucket or not key or not region:
        return False, "URL not parseable as S3"
    access = cfg.get("aws_access_key", "").strip()
    secret = cfg.get("aws_secret_key", "").strip()
    if not access or not secret:
        return False, "AWS credentials missing"
    proxy = _resolve_proxy(db, cfg)
    s3 = make_s3_client(access, secret, region, proxy=proxy)
    body = _redirect_html(new_target, bot_filter=bot_filter).encode("utf-8")
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=body,
                      ContentType="text/html; charset=utf-8",
                      CacheControl="no-cache")
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


@router.post("/redirects/{rid}/update-target", response_class=HTMLResponse)
async def update_link_target(request: Request, rid: int,
                              new_target: str = Form(""),
                              bot_filter: str = Form("")):
    new_target = new_target.strip()
    if not new_target:
        return HTMLResponse('<span style="color:var(--red)">Empty target</span>')
    if not (new_target.startswith("http://") or new_target.startswith("https://")):
        return HTMLResponse('<span style="color:var(--red)">Target must start with http:// or https://</span>')
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM trans_redirect_links WHERE id=?", (rid,)).fetchone()
    if not row:
        return HTMLResponse('<span style="color:var(--red)">Not found</span>')
    cfg = db.get_config()
    ok, err = _re_upload_redirect(db, cfg, dict(row), new_target, bool(bot_filter))
    if not ok:
        return HTMLResponse(f'<span style="color:var(--red)">Failed: {escape(err)}</span>')
    db._conn().execute("UPDATE trans_redirect_links SET target_url=? WHERE id=?",
                        (new_target, rid))
    db._conn().commit()
    return HTMLResponse(f'<span class="badge badge-running">Updated</span>')


_pool_update_progress = {"running": False, "total": 0, "done": 0,
                          "ok": 0, "errors": 0, "pool_id": 0}


@router.post("/redirects/pool/{pid}/update-targets", response_class=HTMLResponse)
async def update_pool_targets(request: Request, pid: int,
                               new_target: str = Form(""),
                               bot_filter: str = Form("")):
    new_target = new_target.strip()
    if not new_target:
        return HTMLResponse('<div class="alert alert-warning">Empty target.</div>')
    if not (new_target.startswith("http://") or new_target.startswith("https://")):
        return HTMLResponse('<div class="alert alert-warning">Target must start with http:// or https://</div>')
    if _pool_update_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">Pool update already running.</div>')

    db = request.app.state.db
    cfg = db.get_config()
    use_bot_filter = bool(bot_filter)
    rows = [dict(r) for r in db.get_redirects_by_pool(pid)]
    if not rows:
        return HTMLResponse('<div class="alert alert-warning">Pool is empty.</div>')

    _pool_update_progress.update(running=True, total=len(rows), done=0,
                                  ok=0, errors=0, pool_id=pid)

    def worker():
        try:
            for r in rows:
                ok, err = _re_upload_redirect(db, cfg, r, new_target, use_bot_filter)
                if ok:
                    db._conn().execute("UPDATE trans_redirect_links SET target_url=? WHERE id=?",
                                        (new_target, r["id"]))
                    db._conn().commit()
                    _pool_update_progress["ok"] += 1
                else:
                    _pool_update_progress["errors"] += 1
                    logger.warning("Pool target update failed for %d: %s", r["id"], err)
                _pool_update_progress["done"] += 1
        finally:
            _pool_update_progress["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Updating {len(rows)} links in pool to {escape(new_target)}…</div>'
        f'<div hx-get="/redirects/pool/{pid}/update-status" hx-trigger="every 2s" hx-swap="outerHTML"></div>'
    )


@router.get("/redirects/pool/{pid}/update-status", response_class=HTMLResponse)
async def pool_update_status(request: Request, pid: int):
    p = _pool_update_progress
    if p["pool_id"] != pid:
        return HTMLResponse("")
    if p["running"]:
        pct = int(p["done"] / p["total"] * 100) if p["total"] else 0
        return HTMLResponse(
            f'<div hx-get="/redirects/pool/{pid}/update-status" hx-trigger="every 2s" hx-swap="outerHTML">'
            f'<div class="progress" style="margin-bottom:6px">'
            f'<div class="progress-bar" style="width:{pct}%">{p["done"]}/{p["total"]}</div></div>'
            f'<p style="font-size:12px;color:var(--fg2)">Re-uploading objects…</p></div>'
        )
    if p["ok"] > 0 or p["errors"] > 0:
        return HTMLResponse(
            f'<div class="alert alert-success">Done. {p["ok"]} updated, {p["errors"]} errors. '
            f'<a href="/redirects" style="color:var(--accent)">Reload</a></div>'
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
    return HTMLResponse("", status_code=200)


@router.post("/redirects/{rid}/set-pool", response_class=HTMLResponse)
async def set_redirect_pool(request: Request, rid: int,
                             pool_id: int = Form(0)):
    db = request.app.state.db
    uid = request.state.user["id"]
    db._conn().execute(
        "UPDATE trans_redirect_links SET pool_id=? WHERE id=?",
        (int(pool_id or 0), rid),
    )
    db._conn().commit()

    # Re-render the cell with the updated dropdown so the change sticks
    # visibly and a follow-up change re-submits cleanly.
    pools = [dict(p) for p in db.get_redirect_pools(uid)]
    cur = int(pool_id or 0)
    options = [f'<option value="0"{" selected" if not cur else ""}>— No Pool</option>']
    for p in pools:
        sel = " selected" if p["id"] == cur else ""
        options.append(f'<option value="{p["id"]}"{sel}>{escape(p["name"])}</option>')
    return HTMLResponse(
        f'<select hx-post="/redirects/{rid}/set-pool" '
        f'hx-target="#pool-cell-{rid}" hx-swap="innerHTML" hx-trigger="change" '
        f'name="pool_id" style="margin:0;padding:2px 4px;font-size:11px;max-width:160px">'
        + "".join(options) +
        '</select>'
    )


@router.post("/redirects/bulk-assign-pool", response_class=HTMLResponse)
async def bulk_assign_pool(request: Request):
    db = request.app.state.db
    form = await request.form()
    pool_id = int(form.get("pool_id", 0) or 0)
    raw_ids = form.getlist("link_ids[]") or form.getlist("link_ids")
    ids = []
    for v in raw_ids:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            pass
    if not ids:
        return HTMLResponse('<span style="color:var(--red)">No links selected.</span>')
    placeholders = ",".join("?" for _ in ids)
    db._conn().execute(
        f"UPDATE trans_redirect_links SET pool_id=? WHERE id IN ({placeholders})",
        [pool_id, *ids],
    )
    db._conn().commit()
    target = "No Pool"
    if pool_id:
        row = db._conn().execute(
            "SELECT name FROM trans_redirect_pools WHERE id=?", (pool_id,)).fetchone()
        if row:
            target = dict(row)["name"]
    return HTMLResponse(
        f'<span style="color:var(--green)">'
        f'Moved {len(ids)} link(s) &rarr; <strong>{escape(target)}</strong>. '
        f'<a href="/redirects" style="color:var(--accent)">Reload</a></span>'
    )


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
