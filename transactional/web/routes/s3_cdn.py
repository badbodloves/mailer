"""S3 als CDN-Quelle für Logo-URLs. Nutzt die bestehenden
trans_s3_accounts (die auch für Redirects verwendet werden) — dein
existierender IAM-Key funktioniert direkt hier weiter. Wir hängen nur
das buckets-Feld pro Account dran und bieten Auto-Bucket-Setup.

Multi-Bucket-Rotation: pro Upload wird ein Bucket round-robin gewählt →
Origin-Domain der URLs variiert (bucket-a.s3.eu-central-1... vs
bucket-b.s3.us-east-1...). Kombiniert mit Cloudinary im CDN-Pool ergibt
das maximale Origin-Diversität für Anti-Cluster."""
import io
import os
import secrets
import time
import threading
import logging
import random
import mimetypes
from html import escape

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()
logger = logging.getLogger("trans.s3")

UPLOAD_TMP = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "static", "uploads", "s3_src"))
os.makedirs(UPLOAD_TMP, exist_ok=True)

_progress = {}


def _prog(uid: int) -> dict:
    p = _progress.get(uid)
    if not p:
        p = {"running": False, "done": 0, "total": 0, "ok": 0, "errors": 0,
             "log": [], "upload_id": 0}
        _progress[uid] = p
    return p


def _tweaked_bytes(src_path: str, seed: int) -> bytes:
    try:
        from PIL import Image
    except ImportError:
        return open(src_path, "rb").read()
    try:
        img = Image.open(src_path)
        fmt = (img.format or "PNG").upper()
        if img.mode == "P":
            img = img.convert("RGBA")
        elif img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        rng = random.Random(seed)
        w, h = img.size
        x = rng.randint(1, max(1, w - 2))
        y = rng.randint(1, max(1, h - 2))
        px = list(img.getpixel((x, y)))
        for i in range(min(3, len(px))):
            px[i] = max(0, min(255, int(px[i]) + rng.choice([-1, 1])))
        img.putpixel((x, y), tuple(px))
        buf = io.BytesIO()
        if fmt == "JPEG":
            img.convert("RGB").save(buf, "JPEG", quality=95, optimize=True)
        elif fmt == "WEBP":
            img.save(buf, "WEBP", quality=95)
        else:
            img.save(buf, "PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.warning("pixel tweak failed: %s", e)
        return open(src_path, "rb").read()


def _proxy_for_account(db, acc: dict) -> str:
    pid = int(acc.get("proxy_id") or 0)
    if not pid:
        return ""
    row = db.get_proxy(pid)
    if not row:
        return ""
    val = (dict(row).get("value") or "").strip()
    return val.splitlines()[0].strip() if val else ""


@router.get("/s3-logos", response_class=HTMLResponse)
async def s3_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    accounts = [dict(a) for a in db.get_s3_accounts(uid)]
    uploads = []
    for u in db.get_s3_uploads(uid):
        ud = dict(u)
        ud["links"] = [dict(l) for l in db.get_s3_links(u["id"])]
        uploads.append(ud)
    return request.app.state.templates.TemplateResponse(request, "s3_cdn.html", {
        "active": "s3_logos",
        "accounts": accounts,
        "uploads": uploads,
        "pool_size": len(db.get_all_cdn_urls(uid)),
    })


@router.post("/s3-logos/accounts/{aid}/setup-bucket", response_class=HTMLResponse)
async def setup_bucket(request: Request, aid: int,
                        bucket: str = Form(""),
                        region: str = Form("eu-central-1"),
                        add_to_pool: int = Form(1)):
    """Full-Auto: Bucket erstellen (falls fehlt), Public-Access-Config
    setzen, Public-Read Policy anhängen. Danach zur Bucket-Liste
    des Accounts hinzufügen."""
    db = request.app.state.db
    uid = request.state.user["id"]
    row = db.get_s3_account(aid)
    if not row:
        return HTMLResponse('<div class="alert alert-danger">Account nicht gefunden.</div>')
    row = dict(row)
    if row.get("user_id", 0) not in (uid, 0):
        return HTMLResponse('<div class="alert alert-danger">Kein Zugriff auf diesen Account.</div>')
    bucket = "".join(c for c in bucket.strip().lower()
                       if c.isalnum() or c in ".-")
    region = region.strip() or "eu-central-1"
    if not bucket or len(bucket) < 3:
        return HTMLResponse('<div class="alert alert-warning">Bucket-Name '
                             'zu kurz (min. 3 Zeichen, nur a-z 0-9 . -).</div>')
    from mailer.s3_uploader import s3_setup_bucket, s3_ping
    proxy = _proxy_for_account(db, row)
    r = s3_setup_bucket(row["access_key"], row["secret_key"], region,
                         bucket, proxy=proxy)
    if not r.get("ok"):
        return HTMLResponse(
            f'<div class="alert alert-danger">Setup fehlgeschlagen: '
            f'{escape(r.get("error", ""))}</div>'
        )
    p = s3_ping(row["access_key"], row["secret_key"], region, bucket, proxy=proxy)
    if not p.get("ok"):
        return HTMLResponse(
            f'<div class="alert alert-warning">Setup lief, aber Test-PUT '
            f'schlug fehl: {escape(p.get("error", ""))}</div>'
        )
    if add_to_pool:
        current = (row.get("buckets") or "").strip()
        new_entry = f"{bucket}:{region}"
        entries = set()
        for e in current.replace("\n", ",").split(","):
            e = e.strip()
            if e:
                entries.add(e)
        if new_entry not in entries:
            entries.add(new_entry)
            new_buckets = "\n".join(sorted(entries))
            db.update_s3_account_buckets(aid, new_buckets, uid)
    steps_html = "".join(
        f'<li>{s["step"]}: {escape(str(s["result"]))}</li>'
        for s in r.get("steps", [])
    )
    return HTMLResponse(
        f'<div class="alert alert-success">✓ Bucket <code>{escape(bucket)}</code> '
        f'in <code>{escape(region)}</code> ist ready.</div>'
        f'<ul style="font-size:11px">{steps_html}</ul>'
        + ('<div class="form-help">Zur Bucket-Liste hinzugefügt — Reload für neue Übersicht.</div>'
           if add_to_pool else "")
    )


@router.post("/s3-logos/accounts/{aid}/test-buckets", response_class=HTMLResponse)
async def test_buckets(request: Request, aid: int):
    db = request.app.state.db
    row = db.get_s3_account(aid)
    if not row:
        return HTMLResponse('<span style="color:var(--red)">Account nicht gefunden</span>')
    row = dict(row)
    from mailer.s3_uploader import parse_buckets_field, s3_ping
    buckets = parse_buckets_field(row.get("buckets", ""))
    if not buckets:
        return HTMLResponse('<span style="color:var(--fg2);font-size:12px">Keine Buckets konfiguriert — nutze „Bucket auto-anlegen" oben.</span>')
    proxy = _proxy_for_account(db, row)
    results = []
    for b, r in buckets:
        p = s3_ping(row["access_key"], row["secret_key"], r, b, proxy=proxy)
        if p.get("ok"):
            results.append(f'<div style="color:var(--green);font-size:12px">✓ <code>{escape(b)}</code> ({escape(r)})</div>')
        else:
            results.append(f'<div style="color:var(--red);font-size:12px">✗ <code>{escape(b)}</code>: {escape(p.get("error", "")[:120])}</div>')
    return HTMLResponse("".join(results))


@router.post("/s3-logos/upload", response_class=HTMLResponse)
async def upload_logo(request: Request,
                        account_id: int = Form(0),
                        file: UploadFile = File(None),
                        count: int = Form(10),
                        base_name: str = Form(""),
                        pixel_tweak: str = Form("1")):
    db = request.app.state.db
    uid = request.state.user["id"]
    prog = _prog(uid)
    if prog["running"]:
        return HTMLResponse('<div class="alert alert-warning">Anderer Upload läuft noch.</div>')
    if not file or not file.filename:
        return HTMLResponse('<div class="alert alert-warning">Keine Datei gewählt.</div>')
    acc = db.get_s3_account(account_id)
    if not acc:
        return HTMLResponse('<div class="alert alert-danger">S3-Account nicht gefunden.</div>')
    acc = dict(acc)
    from mailer.s3_uploader import parse_buckets_field, s3_upload_object, S3Error
    buckets = parse_buckets_field(acc.get("buckets", ""))
    if not buckets:
        return HTMLResponse('<div class="alert alert-danger">Account hat keine Buckets konfiguriert. Nutze „Bucket auto-anlegen" oder trag welche unter /redirects manuell ein.</div>')

    count = max(1, min(int(count or 1), 500))
    base_name = "".join(c for c in (base_name or "").strip()
                          if c.isalnum() or c in "-_") or "logo"
    tweak = bool(int(pixel_tweak or 0))

    safe = "".join(c for c in file.filename if c.isalnum() or c in ".-_") or "upload.bin"
    src_path = os.path.join(UPLOAD_TMP, f"{int(time.time())}_{secrets.token_hex(4)}_{safe}")
    raw = await file.read()
    with open(src_path, "wb") as fh:
        fh.write(raw)

    ext = os.path.splitext(safe)[1].lower() or ".png"
    content_type = mimetypes.guess_type(safe)[0] or "image/png"
    upload_id = db.add_s3_upload(account_id, file.filename, uid)

    prog.update(running=True, done=0, total=count, ok=0, errors=0,
                log=[], upload_id=upload_id)

    proxy = _proxy_for_account(db, acc)

    def worker():
        try:
            for i in range(count):
                bucket, region = buckets[i % len(buckets)]
                key = f"{base_name}/{secrets.token_hex(6)}{ext}"
                try:
                    body = _tweaked_bytes(src_path, seed=i) if tweak else raw
                    url = s3_upload_object(
                        acc["access_key"], acc["secret_key"], region,
                        bucket, key, body, content_type=content_type,
                        public=True, proxy=proxy, timeout=45)
                    db.add_s3_link(upload_id, url, bucket, key)
                    prog["ok"] += 1
                except Exception as e:
                    prog["errors"] += 1
                    prog["log"].append(f"#{i + 1}: {str(e)[:200]}")
                prog["done"] = i + 1
        finally:
            prog["running"] = False
            try:
                os.unlink(src_path)
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()
    return HTMLResponse(
        f'<div class="alert alert-info">Lade {count} Varianten hoch, verteilt '
        f'über {len(buckets)} Bucket(s)…</div>'
        f'<div hx-get="/s3-logos/progress" hx-trigger="every 1s" hx-swap="outerHTML"></div>'
    )


@router.get("/s3-logos/progress", response_class=HTMLResponse)
async def progress(request: Request):
    uid = request.state.user["id"]
    p = _prog(uid)
    if p["running"]:
        pct = int(p["done"] / p["total"] * 100) if p["total"] else 0
        return HTMLResponse(
            f'<div hx-get="/s3-logos/progress" hx-trigger="every 1s" hx-swap="outerHTML">'
            f'<div class="progress" style="margin-bottom:6px">'
            f'<div class="progress-bar" style="width:{pct}%">{p["done"]}/{p["total"]}</div></div>'
            f'<p style="font-size:12px;color:var(--fg2)">'
            f'{p["ok"]} hochgeladen, {p["errors"]} Fehler</p></div>'
        )
    if p["ok"] > 0 or p["errors"] > 0:
        err = ""
        if p["log"]:
            err = ('<details style="margin-top:8px"><summary>Fehler-Log</summary>'
                    '<pre style="font-size:11px;max-height:200px;overflow:auto">'
                    + escape("\n".join(p["log"])) + '</pre></details>')
        color = "success" if p["errors"] == 0 else "warning"
        err_frag = f', {p["errors"]} Fehler' if p["errors"] else ""
        result = HTMLResponse(
            f'<div class="alert alert-{color}">Fertig: {p["ok"]}/{p["total"]} hochgeladen'
            f'{err_frag}. '
            f'<a href="/s3-logos" style="color:var(--accent)">Reload</a></div>'
            + err
        )
        p["ok"] = 0
        p["errors"] = 0
        p["done"] = 0
        p["total"] = 0
        p["log"] = []
        return result
    return HTMLResponse('')


@router.post("/s3-logos/uploads/{upload_id}/delete")
async def delete_upload(request: Request, upload_id: int):
    db = request.app.state.db
    uid = request.state.user["id"]
    db.delete_s3_upload(upload_id, uid)
    return RedirectResponse("/s3-logos", status_code=303)
