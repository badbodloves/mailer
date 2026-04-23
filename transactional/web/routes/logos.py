"""Logos — upload, list, delete, generate variants."""
import os
import time
import threading
import logging
import secrets
from html import escape
from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List as TList

logger = logging.getLogger("trans.logos")
router = APIRouter()

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "static", "uploads", "logos")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

_variant_progress = {"running": False, "done": 0, "total": 0, "error": ""}


@router.get("/logos", response_class=HTMLResponse)
async def logos_page(request: Request):
    db = request.app.state.db
    logos = [dict(l) for l in db.get_logos()]
    return request.app.state.templates.TemplateResponse(request, "logos.html", {
        "active": "logos", "logos": logos, "db": db,
        "variant_running": _variant_progress["running"],
    })


@router.post("/logos/upload")
async def upload_logos(request: Request,
                       files: TList[UploadFile] = File(...)):
    db = request.app.state.db
    for f in files:
        if not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            continue
        data = await f.read()
        if not data or len(data) > MAX_FILE_SIZE:
            continue
        safe_name = f"{int(time.time())}_{secrets.token_hex(4)}{ext}"
        dest = os.path.join(UPLOAD_DIR, safe_name)
        with open(dest, "wb") as fh:
            fh.write(data)
        rel_path = f"/static/uploads/logos/{safe_name}"
        db.add_logo(f.filename, rel_path)
    return RedirectResponse("/logos", status_code=303)


@router.post("/logos/{lid}/delete")
async def delete_logo(request: Request, lid: int):
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM trans_logos WHERE id=?", (lid,)).fetchone()
    if row:
        row = dict(row)
        file_path = row.get("file_path", "")
        # Handle both absolute paths and relative /static/ paths
        if file_path.startswith("/static/uploads/logos/"):
            abs_path = os.path.join(
                os.path.dirname(__file__), "..",
                file_path.lstrip("/").split("/", 1)[0],
                *file_path.lstrip("/").split("/")[1:]
            )
            abs_path = os.path.normpath(abs_path)
        else:
            abs_path = file_path
        if os.path.isfile(abs_path):
            try:
                os.unlink(abs_path)
            except OSError:
                pass
        db.delete_logo(lid)
    return RedirectResponse("/logos", status_code=303)


@router.post("/logos/generate-variants", response_class=HTMLResponse)
async def generate_variants(request: Request):
    if _variant_progress["running"]:
        return HTMLResponse(
            '<div class="alert alert-warning">Variant generation already running.</div>'
        )

    db = request.app.state.db
    logos = [dict(l) for l in db.get_logos()]
    if not logos:
        return HTMLResponse(
            '<div class="alert alert-warning">No logos uploaded. Upload logos first.</div>'
        )

    _variant_progress.update(running=True, done=0, total=len(logos), error="")

    def worker():
        try:
            from PIL import Image, ImageEnhance, ImageFilter
        except ImportError:
            _variant_progress["error"] = "Pillow is not installed"
            _variant_progress["running"] = False
            return

        import random

        try:
            for idx, logo in enumerate(logos):
                file_path = logo.get("file_path", "")
                if file_path.startswith("/static/uploads/logos/"):
                    abs_path = os.path.join(
                        os.path.dirname(__file__), "..",
                        file_path.lstrip("/").split("/", 1)[0],
                        *file_path.lstrip("/").split("/")[1:]
                    )
                    abs_path = os.path.normpath(abs_path)
                else:
                    abs_path = file_path

                if not os.path.isfile(abs_path):
                    _variant_progress["done"] = idx + 1
                    continue

                try:
                    img = Image.open(abs_path)
                    if img.mode == "P":
                        img = img.convert("RGBA")
                    elif img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGB")

                    for v in range(3):
                        variant = img.copy()
                        variant = ImageEnhance.Brightness(variant).enhance(
                            1.0 + random.uniform(-0.05, 0.05))
                        variant = ImageEnhance.Color(variant).enhance(
                            1.0 + random.uniform(-0.05, 0.05))
                        variant = ImageEnhance.Contrast(variant).enhance(
                            1.0 + random.uniform(-0.02, 0.02))

                        ext = os.path.splitext(abs_path)[1].lower()
                        if ext not in (".png", ".jpg", ".jpeg"):
                            ext = ".png"
                        vname = f"{int(time.time())}_{secrets.token_hex(4)}_v{v}{ext}"
                        vpath = os.path.join(UPLOAD_DIR, vname)
                        fmt = "PNG" if ext == ".png" else "JPEG"
                        save_kw = {"optimize": True}
                        if fmt == "JPEG":
                            save_kw["quality"] = 95
                        variant.save(vpath, format=fmt, **save_kw)
                        rel = f"/static/uploads/logos/{vname}"
                        db.add_logo(f"{logo['filename']}_v{v}", rel)

                except Exception as e:
                    logger.error("Variant gen error for %s: %s", logo["filename"], e)

                _variant_progress["done"] = idx + 1
                time.sleep(0.1)

        except Exception as e:
            logger.error("Variant generation failed: %s", e, exc_info=True)
            _variant_progress["error"] = str(e)[:200]
        finally:
            _variant_progress["running"] = False

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return HTMLResponse(
        f'<div class="alert alert-info">Generating variants for {len(logos)} logo(s)...</div>'
        f'<div id="variant-progress" hx-get="/logos/variant-status" '
        f'hx-trigger="every 2s" hx-swap="innerHTML"></div>'
    )


@router.get("/logos/variant-status", response_class=HTMLResponse)
async def variant_status(request: Request):
    p = _variant_progress
    if p["error"]:
        return HTMLResponse(
            f'<div class="alert alert-danger">Error: {escape(p["error"])}</div>'
        )
    if not p["running"] and p["total"] == 0:
        return HTMLResponse("")

    done = p["done"]
    total = p["total"]
    pct = int(done / total * 100) if total > 0 else 0

    if p["running"]:
        return HTMLResponse(
            f'<div class="progress"><div class="progress-bar" style="width:{pct}%">'
            f'{done}/{total}</div></div>'
        )
    return HTMLResponse(
        f'<div class="alert alert-success">Done! Generated variants for {total} logo(s). '
        f'<a href="/logos" style="color:var(--accent)">Reload page</a></div>'
    )
