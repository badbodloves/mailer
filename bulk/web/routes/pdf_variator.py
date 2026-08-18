"""PDF Variator — Web-UI + Streaming-ZIP-Download.

Keine Server-seitige Speicherung: Source-PDF landet nur im RAM, jede
Variante wird generiert → in ZIP-Stream gepusht → RAM freigegeben.
Zu jedem Zeitpunkt max ~2 Varianten im Speicher.
"""
import io
import logging
import zipfile
from html import escape
from typing import Optional

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse

logger = logging.getLogger("bulk.pdf_variator")
router = APIRouter()


def _parse_layers(**kw) -> "LayerSet":
    from bulk.mailer.pdf_variator import LayerSet
    ls = LayerSet()
    # A/B/C sind immer an — keine Toggles hier
    ls.image = bool(kw.get("layer_image"))
    ls.byte_noise = bool(kw.get("layer_byte_noise"))
    ls.cmap_poison = bool(kw.get("layer_cmap_poison"))
    ls.hidden_text = bool(kw.get("layer_hidden_text"))
    return ls


def _parse_pools(**kw) -> "PoolBag":
    from bulk.mailer.pdf_variator import PoolBag
    return PoolBag.from_dict({
        "filename": kw.get("pool_filename", ""),
        "producer": kw.get("pool_producer", ""),
        "creator": kw.get("pool_creator", ""),
        "author": kw.get("pool_author", ""),
        "title": kw.get("pool_title", ""),
        "subject": kw.get("pool_subject", ""),
        "keywords": kw.get("pool_keywords", ""),
    })


@router.get("/pdf-variator", response_class=HTMLResponse)
async def pdf_variator_page(request: Request):
    from bulk.mailer.pdf_variator import (FILENAME_POOL, PRODUCER_POOL,
                                            CREATOR_POOL, AUTHOR_POOL,
                                            TITLE_POOL, SUBJECT_POOL,
                                            KEYWORDS_POOL)
    # Check pikepdf availability early
    try:
        import pikepdf  # noqa
        pikepdf_ok = True
        pike_ver = pikepdf.__version__
    except ImportError:
        pikepdf_ok = False
        pike_ver = None

    return request.app.state.templates.TemplateResponse(request, "pdf_variator.html", {
        "active": "pdf_variator",
        "pikepdf_ok": pikepdf_ok, "pike_ver": pike_ver,
        "pool_filename": "\n".join(FILENAME_POOL),
        "pool_producer": "\n".join(PRODUCER_POOL),
        "pool_creator": "\n".join(CREATOR_POOL),
        "pool_author": "\n".join(AUTHOR_POOL),
        "pool_title": "\n".join(TITLE_POOL),
        "pool_subject": "\n".join(SUBJECT_POOL),
        "pool_keywords": ", ".join(KEYWORDS_POOL),
    })


@router.post("/pdf-variator/test-report", response_class=HTMLResponse)
async def test_report(request: Request, source: UploadFile = File(...),
                       layer_image: str = Form(""),
                       layer_byte_noise: str = Form(""),
                       layer_cmap_poison: str = Form(""),
                       layer_hidden_text: str = Form(""),
                       pool_filename: str = Form(""),
                       pool_producer: str = Form(""),
                       pool_creator: str = Form(""),
                       pool_author: str = Form(""),
                       pool_title: str = Form(""),
                       pool_subject: str = Form(""),
                       pool_keywords: str = Form("")):
    try:
        from bulk.mailer.pdf_variator import PDFVariator
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">pikepdf fehlt: {escape(str(e))}</div>')

    src = await source.read()
    if not src or len(src) > 50 * 1024 * 1024:
        return HTMLResponse('<div class="alert alert-warning">PDF fehlt oder > 50 MB.</div>')
    if not src.startswith(b"%PDF"):
        return HTMLResponse('<div class="alert alert-warning">Sieht nicht nach PDF aus (kein %PDF-Header).</div>')

    layers = _parse_layers(layer_image=layer_image, layer_byte_noise=layer_byte_noise,
                            layer_cmap_poison=layer_cmap_poison,
                            layer_hidden_text=layer_hidden_text)
    pools = _parse_pools(pool_filename=pool_filename, pool_producer=pool_producer,
                          pool_creator=pool_creator, pool_author=pool_author,
                          pool_title=pool_title, pool_subject=pool_subject,
                          pool_keywords=pool_keywords)
    try:
        v = PDFVariator(src, layers=layers, pools=pools)
        rows = v.compare_variants(3)
    except Exception as e:
        logger.exception("test-report crashed")
        return HTMLResponse(f'<div class="alert alert-danger">Fehler: {escape(str(e))}</div>')

    rows_html = []
    for r in rows:
        rows_html.append(
            f'<tr>'
            f'<td>{escape(r["which"])}</td>'
            f'<td style="font-family:monospace;font-size:11px">{escape(r["filename"])}</td>'
            f'<td>{r["size"]:,}</td>'
            f'<td style="font-family:monospace;font-size:10px">{escape(r["md5"])}</td>'
            f'<td style="font-family:monospace;font-size:10px">{escape(r["sha256"][:24])}…</td>'
            f'</tr>'
        )
    all_hashes = [r["md5"] for r in rows[1:]]  # nur die Varianten
    all_unique = len(set(all_hashes)) == len(all_hashes)
    verdict_html = (
        '<div class="alert alert-success">✓ Alle 3 Varianten haben unterschiedliche MD5s.</div>'
        if all_unique else
        '<div class="alert alert-danger">✗ Duplikate! Layer nachziehen (Image + Byte-Noise aktivieren).</div>'
    )
    return HTMLResponse(
        verdict_html +
        '<table style="font-size:11px"><thead>'
        '<tr><th>#</th><th>Filename</th><th>Bytes</th><th>MD5</th><th>SHA256 (Kurz)</th></tr>'
        '</thead><tbody>' + "".join(rows_html) + '</tbody></table>'
        '<p class="muted" style="margin-top:6px">'
        'Wenn die MD5s der 3 Varianten schon so weit auseinander sind → auch '
        'ssdeep/tlsh/pHash werden es. Kein Grund vorher mit größeren Batches zu testen.'
        '</p>'
    )


@router.post("/pdf-variator/generate")
async def generate(request: Request,
                    source: UploadFile = File(...),
                    count: int = Form(100),
                    layer_image: str = Form(""),
                    layer_byte_noise: str = Form(""),
                    layer_cmap_poison: str = Form(""),
                    layer_hidden_text: str = Form(""),
                    pool_filename: str = Form(""),
                    pool_producer: str = Form(""),
                    pool_creator: str = Form(""),
                    pool_author: str = Form(""),
                    pool_title: str = Form(""),
                    pool_subject: str = Form(""),
                    pool_keywords: str = Form("")):
    try:
        from bulk.mailer.pdf_variator import PDFVariator
    except Exception as e:
        return PlainTextResponse(f"pikepdf fehlt: {e}", status_code=500)

    src = await source.read()
    if not src or len(src) > 50 * 1024 * 1024:
        return PlainTextResponse("PDF fehlt oder > 50 MB", status_code=400)
    if not src.startswith(b"%PDF"):
        return PlainTextResponse("Kein %PDF-Header", status_code=400)

    count = max(1, min(int(count or 100), 1000))
    layers = _parse_layers(layer_image=layer_image, layer_byte_noise=layer_byte_noise,
                            layer_cmap_poison=layer_cmap_poison,
                            layer_hidden_text=layer_hidden_text)
    pools = _parse_pools(pool_filename=pool_filename, pool_producer=pool_producer,
                          pool_creator=pool_creator, pool_author=pool_author,
                          pool_title=pool_title, pool_subject=pool_subject,
                          pool_keywords=pool_keywords)
    variator = PDFVariator(src, layers=layers, pools=pools)

    # Filename-Kollisionen aktiv vermeiden — pro ZIP kein Duplikat
    used_names = set()

    def _generate_stream():
        """Yield chunks of a growing ZIP file. Buffer = one variant at a time."""
        buf = io.BytesIO()
        # ZIP_STORED — PDFs sind intern schon komprimiert, kein Doppel-Compress
        zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED, allowZip64=True)

        def _flush():
            data = buf.getvalue()
            buf.seek(0)
            buf.truncate()
            return data

        for i in range(count):
            try:
                fname, pdf_bytes = variator.make_variant(seed=i)
            except Exception as e:
                logger.warning("Variant %d failed: %s", i, e)
                continue
            # Kollision? → numerischen Suffix ranhängen
            base_fname = fname
            n = 1
            while fname in used_names:
                stem, ext = base_fname.rsplit(".", 1)
                fname = f"{stem}_{n}.{ext}"
                n += 1
            used_names.add(fname)

            zi = zipfile.ZipInfo(fname)
            zi.compress_type = zipfile.ZIP_STORED
            zf.writestr(zi, pdf_bytes)
            yield _flush()
        zf.close()
        yield _flush()

    return StreamingResponse(
        _generate_stream(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename=pdf_variants_{count}.zip'},
    )
