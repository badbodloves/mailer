"""Logos — upload source logos, generate variants to separate dir, cleanup."""
import os
import time
import shutil
import threading
import logging
import secrets
from html import escape
from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List as TList

logger = logging.getLogger("trans.logos")
router = APIRouter()

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "static", "uploads", "logos")
VARIANT_DIR = os.path.join(os.path.dirname(__file__), "..", "static", "uploads", "logo_variants")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(VARIANT_DIR, exist_ok=True)

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

_variant_progress = {"running": False, "done": 0, "total": 0, "count": 0, "error": ""}


def _group_variant_dir(group_id: int = 0) -> str:
    if group_id:
        d = os.path.join(VARIANT_DIR, f"group_{group_id}")
    else:
        d = VARIANT_DIR
    os.makedirs(d, exist_ok=True)
    return d


def _resolve_path(file_path: str) -> str:
    if file_path.startswith("/static/"):
        return os.path.normpath(os.path.join(
            os.path.dirname(__file__), "..", file_path.lstrip("/")))
    return file_path


def get_variant_count() -> int:
    try:
        count = 0
        for root, dirs, files in os.walk(VARIANT_DIR):
            count += sum(1 for f in files if not f.startswith("."))
        return count
    except OSError:
        return 0


def get_group_variant_count(group_id: int) -> int:
    d = _group_variant_dir(group_id)
    try:
        return sum(1 for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))
    except OSError:
        return 0


def clear_variants():
    try:
        shutil.rmtree(VARIANT_DIR)
        os.makedirs(VARIANT_DIR, exist_ok=True)
    except OSError:
        pass


@router.get("/logos", response_class=HTMLResponse)
async def logos_page(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    logos = [dict(l) for l in db.get_logos(uid)]
    logo_groups = [dict(g) for g in db.get_logo_groups(uid)]
    for g in logo_groups:
        g["variant_count"] = get_group_variant_count(g["id"])
    return request.app.state.templates.TemplateResponse(request, "logos.html", {
        "active": "logos", "logos": logos, "logo_groups": logo_groups, "db": db,
        "variant_running": _variant_progress["running"],
        "variant_count": get_variant_count(),
    })


@router.post("/logos/add-group")
async def add_logo_group(request: Request, name: str = Form("")):
    if name.strip():
        uid = request.state.user["id"]
        request.app.state.db.add_logo_group(name.strip(), uid)
    return RedirectResponse("/logos", status_code=303)


@router.post("/logos/group/{gid}/delete")
async def delete_logo_group(request: Request, gid: int):
    request.app.state.db.delete_logo_group(gid)
    return RedirectResponse("/logos", status_code=303)


@router.post("/logos/upload")
async def upload_logos(request: Request):
    form = await request.form()
    files = form.getlist("files")
    group_id = int(form.get("group_id", 0) or 0)
    db = request.app.state.db
    uid = request.state.user['id']
    for f in files:
        if not hasattr(f, "read") or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            continue
        data = await f.read()
        if not data or len(data) > 5 * 1024 * 1024:
            continue
        safe = f"{int(time.time())}_{secrets.token_hex(4)}{ext}"
        dest = os.path.join(UPLOAD_DIR, safe)
        with open(dest, "wb") as fh:
            fh.write(data)
        db.add_logo(f.filename, f"/static/uploads/logos/{safe}", uid, group_id)
    return RedirectResponse("/logos", status_code=303)


@router.post("/logos/{lid}/delete")
async def delete_logo(request: Request, lid: int):
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM trans_logos WHERE id=?", (lid,)).fetchone()
    if row:
        abs_path = _resolve_path(dict(row).get("file_path", ""))
        if os.path.isfile(abs_path):
            try:
                os.unlink(abs_path)
            except OSError:
                pass
        db.delete_logo(lid)
    return RedirectResponse("/logos", status_code=303)


@router.post("/logos/generate-variants", response_class=HTMLResponse)
async def generate_variants(request: Request, variant_count: int = Form(25),
                             group_id: int = Form(0)):
    if _variant_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">Already running.</div>')

    db = request.app.state.db
    uid = request.state.user['id']
    if group_id:
        logos = [dict(l) for l in db.get_logos_by_group(group_id)]
    else:
        logos = [dict(l) for l in db.get_logos(uid)]
    if not logos:
        return HTMLResponse('<div class="alert alert-warning">No logos in this group.</div>')

    variant_dir = _group_variant_dir(group_id)
    if os.path.isdir(variant_dir):
        shutil.rmtree(variant_dir)
    os.makedirs(variant_dir, exist_ok=True)

    per_logo = max(1, variant_count // len(logos))
    _variant_progress.update(running=True, done=0, total=len(logos) * per_logo, count=0, error="")

    def worker():
        try:
            from PIL import Image, ImageEnhance
        except ImportError:
            _variant_progress["error"] = "Pillow not installed"
            _variant_progress["running"] = False
            return

        import random
        generated = 0
        try:
            for logo in logos:
                abs_path = _resolve_path(logo.get("file_path", ""))
                if not os.path.isfile(abs_path):
                    continue
                try:
                    img = Image.open(abs_path)
                    if img.mode == "P":
                        img = img.convert("RGBA")
                    elif img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGB")

                    ext = os.path.splitext(abs_path)[1].lower()
                    if ext not in (".png", ".jpg", ".jpeg"):
                        ext = ".png"
                    fmt = "PNG" if ext == ".png" else "JPEG"

                    for v in range(per_logo):
                        variant = img.copy()
                        variant = ImageEnhance.Brightness(variant).enhance(
                            1.0 + random.uniform(-0.05, 0.05))
                        variant = ImageEnhance.Color(variant).enhance(
                            1.0 + random.uniform(-0.08, 0.08))
                        variant = ImageEnhance.Contrast(variant).enhance(
                            1.0 + random.uniform(-0.03, 0.03))

                        pixels = variant.load()
                        w, h = variant.size
                        px = random.randint(0, w - 1)
                        py = random.randint(0, h - 1)
                        pixel = pixels[px, py]
                        if len(pixel) == 4:
                            p = list(pixel[:3])
                            p[random.randint(0, 2)] = min(255, max(0, p[random.randint(0, 2)] + random.choice([-2, -1, 1, 2])))
                            pixels[px, py] = (p[0], p[1], p[2], pixel[3])
                        elif len(pixel) == 3:
                            p = list(pixel)
                            p[random.randint(0, 2)] = min(255, max(0, p[random.randint(0, 2)] + random.choice([-2, -1, 1, 2])))
                            pixels[px, py] = (p[0], p[1], p[2])

                        if ext == ".png":
                            try:
                                from PIL import Image as PILImage
                                method = (PILImage.Quantize.FASTOCTREE if variant.mode == "RGBA"
                                          else PILImage.Quantize.MEDIANCUT)
                                variant = variant.quantize(colors=256, method=method)
                            except Exception:
                                pass

                        vname = f"v_{generated:04d}_{secrets.token_hex(3)}{ext}"
                        vpath = os.path.join(variant_dir, vname)
                        save_kw = {"optimize": True}
                        if fmt == "PNG":
                            save_kw["compress_level"] = 9
                        elif fmt == "JPEG":
                            save_kw["quality"] = 95
                        variant.save(vpath, format=fmt, **save_kw)
                        generated += 1
                        _variant_progress["done"] = generated
                except Exception as e:
                    logger.error("Variant error for %s: %s", logo["filename"], e)

            _variant_progress["count"] = generated
        except Exception as e:
            _variant_progress["error"] = str(e)[:200]
        finally:
            _variant_progress["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    group_label = f" (group {group_id})" if group_id else " (global)"
    return HTMLResponse(
        f'<div class="alert alert-info">Generating {per_logo} variants per logo ({len(logos)} logos){group_label}...</div>'
        f'<div hx-get="/logos/variant-status" hx-trigger="every 2s" hx-swap="innerHTML"></div>')


@router.get("/logos/variant-status", response_class=HTMLResponse)
async def variant_status(request: Request):
    p = _variant_progress
    if p["error"]:
        return HTMLResponse(f'<div class="alert alert-danger">Error: {escape(p["error"])}</div>')
    if not p["running"] and p["total"] == 0:
        return HTMLResponse("")
    done, total = p["done"], p["total"]
    pct = int(done / total * 100) if total > 0 else 0
    if p["running"]:
        return HTMLResponse(
            f'<div class="progress"><div class="progress-bar" style="width:{pct}%">{done}/{total}</div></div>')
    return HTMLResponse(
        f'<div class="alert alert-success">{p["count"]} variants generated in /logo_variants/. '
        f'<a href="/logos" style="color:var(--accent)">Reload</a></div>')


@router.post("/logos/clear-variants", response_class=HTMLResponse)
async def clear_variant_files(request: Request, group_id: int = Form(0)):
    if group_id:
        d = _group_variant_dir(group_id)
        if os.path.isdir(d):
            shutil.rmtree(d)
            os.makedirs(d, exist_ok=True)
        return HTMLResponse(f'<div class="alert alert-success">Variants for group {group_id} cleared.</div>')
    clear_variants()
    return HTMLResponse('<div class="alert alert-success">All variants cleared.</div>')
