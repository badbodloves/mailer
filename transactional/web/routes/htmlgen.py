"""HTML Content Engine — generate randomized email templates from modular blocks."""
import os
import threading
import logging
from pathlib import Path
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

logger = logging.getLogger("trans.htmlgen")
router = APIRouter()

_HTMLGEN_BASE = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "htmlgen")))
_OUTPUT_DIR = _HTMLGEN_BASE / "output"
_gen_progress = {"running": False, "total": 0, "done": 0}


def _list_generated() -> list[dict]:
    if not _OUTPUT_DIR.is_dir():
        return []
    results = []
    for f in sorted(_OUTPUT_DIR.glob("*.html")):
        results.append({"name": f.stem, "html": f.read_text(encoding="utf-8")})
    return results


@router.get("/htmlgen", response_class=HTMLResponse)
async def htmlgen_page(request: Request):
    from htmlgen.engine import _load_all, BLOCK_NAMES
    from htmlgen.config import load_config

    cfg_path = _HTMLGEN_BASE / "config.yaml"
    cfg = load_config(cfg_path)
    block_variants, layouts = _load_all(_HTMLGEN_BASE)

    blocks_info = []
    for name in BLOCK_NAMES:
        variants = block_variants.get(name, [])
        blocks_info.append({
            "name": name,
            "count": len(variants),
            "enabled": cfg.get("blocks", {}).get(name, True),
        })

    layout_names = [l["name"] for l in layouts]
    generated = _list_generated()

    return request.app.state.templates.TemplateResponse(request, "htmlgen.html", {
        "active": "htmlgen",
        "blocks_info": blocks_info,
        "layout_names": layout_names,
        "cfg": cfg,
        "generated": generated,
        "gen_progress": _gen_progress,
    })


@router.post("/htmlgen/generate", response_class=HTMLResponse)
async def generate_templates(request: Request,
                              count: int = Form(50),
                              layout: str = Form(""),
                              primary_color: str = Form(""),
                              accent_color: str = Form("")):
    if _gen_progress["running"]:
        return HTMLResponse('<div class="alert alert-warning">Generation already running.</div>')

    count = max(1, min(count, 5000))
    _gen_progress.update(running=True, total=count, done=0)

    def worker():
        try:
            from htmlgen.config import load_config
            from htmlgen.engine import generate_one, _load_all

            cfg_path = _HTMLGEN_BASE / "config.yaml"
            cfg = load_config(cfg_path)

            if layout and layout != "random":
                cfg["layout"] = layout
            if primary_color.strip():
                cfg.setdefault("colors", {})["primary"] = [primary_color.strip()]
                from htmlgen.colors import lighten_color
                cfg["colors"]["light_accent_bg"] = [lighten_color(primary_color.strip(), cfg.get("lighten_amount", 0.85))]
            if accent_color.strip():
                cfg.setdefault("colors", {})["accent"] = [accent_color.strip()]

            _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            cache = _load_all(_HTMLGEN_BASE)

            for i in range(1, count + 1):
                html = generate_one(cfg, _HTMLGEN_BASE, _cache=cache)
                (_OUTPUT_DIR / f"template_{i:04d}.html").write_text(html, encoding="utf-8")
                _gen_progress["done"] = i

        except Exception as e:
            logger.error("HTMLGen error: %s", e, exc_info=True)
        finally:
            _gen_progress["running"] = False

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return HTMLResponse(
        f'<div class="alert alert-info">Generating {count} templates...</div>'
        f'<div id="hg-progress" hx-get="/htmlgen/status" '
        f'hx-trigger="every 1s" hx-swap="innerHTML"></div>'
    )


@router.get("/htmlgen/status", response_class=HTMLResponse)
async def gen_status(request: Request):
    p = _gen_progress
    if p["running"]:
        done = p["done"]
        total = p["total"]
        pct = int(done / total * 100) if total > 0 else 0
        return HTMLResponse(
            f'<div class="progress" style="margin-bottom:8px">'
            f'<div class="progress-bar" style="width:{pct}%">{done}/{total}</div></div>'
        )
    if p["done"] > 0:
        return HTMLResponse(
            f'<div class="alert alert-success">Done! {p["done"]} templates generated. '
            f'<a href="/htmlgen" style="color:var(--accent)">Reload page</a></div>'
        )
    return HTMLResponse("")


@router.post("/htmlgen/preview", response_class=HTMLResponse)
async def preview_template(request: Request,
                            layout: str = Form(""),
                            primary_color: str = Form(""),
                            accent_color: str = Form("")):
    from htmlgen.config import load_config
    from htmlgen.engine import generate_one, _load_all

    cfg_path = _HTMLGEN_BASE / "config.yaml"
    cfg = load_config(cfg_path)

    if layout and layout != "random":
        cfg["layout"] = layout
    if primary_color.strip():
        cfg.setdefault("colors", {})["primary"] = [primary_color.strip()]
        from htmlgen.colors import lighten_color
        cfg["colors"]["light_accent_bg"] = [lighten_color(primary_color.strip(), cfg.get("lighten_amount", 0.85))]
    if accent_color.strip():
        cfg.setdefault("colors", {})["accent"] = [accent_color.strip()]

    cache = _load_all(_HTMLGEN_BASE)
    html = generate_one(cfg, _HTMLGEN_BASE, _cache=cache)

    escaped = escape(html)
    return HTMLResponse(
        f'<div style="margin-bottom:12px">'
        f'<button class="btn btn-secondary btn-sm" onclick="'
        f"document.getElementById('preview-source').style.display="
        f"document.getElementById('preview-source').style.display==='none'?'block':'none'"
        f'">Toggle Source</button></div>'
        f'<div style="border:1px solid var(--border);border-radius:4px;overflow:hidden;margin-bottom:12px">'
        f'<iframe srcdoc="{escape(html, quote=True)}" style="width:100%;height:600px;border:none"></iframe></div>'
        f'<pre id="preview-source" style="display:none;background:#f5f5f5;padding:12px;border-radius:4px;'
        f'font-size:12px;overflow-x:auto;max-height:400px">{escaped}</pre>'
    )


@router.post("/htmlgen/export")
async def export_templates(request: Request):
    import io
    import zipfile

    generated = _list_generated()
    if not generated:
        return RedirectResponse("/htmlgen", status_code=303)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, tpl in enumerate(generated, 1):
            zf.writestr(f"template_{i:04d}.html", tpl["html"])
    buf.seek(0)

    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=htmlgen_{len(generated)}.zip"},
    )


@router.post("/htmlgen/load-to-mailer", response_class=HTMLResponse)
async def load_to_mailer(request: Request):
    db = request.app.state.db
    uid = request.state.user["id"]
    generated = _list_generated()

    if not generated:
        return HTMLResponse('<div class="alert alert-warning">No templates to load.</div>')

    loaded = 0
    for i, tpl in enumerate(generated, 1):
        name = f"HtmlGen #{i}"
        db.add_template(name, tpl["html"], uid)
        loaded += 1

    return HTMLResponse(
        f'<div class="alert alert-success">{loaded} templates loaded into '
        f'<a href="/templates" style="color:var(--accent)">HTML Editor</a>.</div>'
    )


@router.post("/htmlgen/clear")
async def clear_templates(request: Request):
    import shutil
    if _OUTPUT_DIR.is_dir():
        shutil.rmtree(_OUTPUT_DIR)
    return RedirectResponse("/htmlgen", status_code=303)
