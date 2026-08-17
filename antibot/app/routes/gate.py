"""Public gate endpoints — the actual bot filter surface."""
import time
import json
import logging
import hashlib
import threading
import requests as _req
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse

from ..tokens import (verify_token, session_bucket_from_request,
                      verify_cookie, issue_verify_cookie)
from ..scoring import score_request

logger = logging.getLogger("antibot.gate")
router = APIRouter()

VERIFY_COOKIE = "abo_verified"


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("x-real-ip", "") or (request.client.host if request.client else "")


def _fire_webhook(cfg: dict, payload: dict):
    url = cfg.get("webhook_url", "").strip()
    if not url:
        return
    def _send():
        try:
            _req.post(url, json=payload, timeout=3)
        except Exception as e:
            logger.warning("webhook fire failed: %s", e)
    threading.Thread(target=_send, daemon=True).start()


@router.get("/go/{token}", response_class=HTMLResponse)
async def gate_entry(request: Request, token: str):
    """Mail-link landing. Validates token → scores → routes."""
    db = request.app.state.db
    cfg = db.get_config()
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    hmac_secret = cfg.get("hmac_secret", "")
    cookie_secret = cfg.get("cookie_secret", "")

    payload = verify_token(hmac_secret, token) if hmac_secret else None
    token_valid = payload is not None
    target = (payload.get("t") if payload else "") or cfg.get("default_target", "")
    if not target:
        return PlainTextResponse("no target configured", status_code=500)

    bucket = session_bucket_from_request(ip, ua, cookie_secret or "salt")

    # Owner bypass — HMAC-signed "?bypass=..." for the owner to test live
    if request.query_params.get("bypass") == _owner_bypass(cookie_secret):
        db.log_decision(ip=ip, asn="", country="", user_agent=ua, target=target,
                        verdict="allow", score=0, signals_json='{"owner_bypass":true}',
                        token_valid=1 if token_valid else 0,
                        dry_run=1 if cfg.get("dry_run") == "1" else 0)
        return RedirectResponse(target, status_code=302)

    # If token invalid AND no default_target fallback wanted, 404
    if not token_valid and not target:
        return PlainTextResponse("not found", status_code=404)

    # Verified cookie shortcut — skip the challenge entirely
    if verify_cookie(cookie_secret, request.cookies.get(VERIFY_COOKIE, ""), bucket):
        db.log_decision(ip=ip, asn="", country="", user_agent=ua, target=target,
                        verdict="allow", score=0,
                        signals_json='{"verify_cookie":true}',
                        token_valid=1 if token_valid else 0,
                        dry_run=1 if cfg.get("dry_run") == "1" else 0)
        return RedirectResponse(target, status_code=302)

    # Score server-side signals
    result = score_request(db, cfg, ip=ip, user_agent=ua, rate_bucket=bucket)
    hint = result["verdict_hint"]
    dry_run = cfg.get("dry_run") == "1"

    # Log every decision
    dec_id = db.log_decision(
        ip=ip, asn=result["network"].get("asn", ""),
        country=result["network"].get("country", ""), user_agent=ua,
        target=target, verdict=hint, score=result["score"],
        signals_json=result["signals"],
        token_valid=1 if token_valid else 0,
        dry_run=1 if dry_run else 0)

    if result["score"] >= int(cfg.get("webhook_min_score", "70")):
        _fire_webhook(cfg, {"id": dec_id, "ip": ip, "score": result["score"],
                             "verdict": hint, "signals": result["signals"],
                             "ts": int(time.time())})

    # DRY-RUN: log verdict but always allow-redirect
    if dry_run:
        return RedirectResponse(target, status_code=302)

    if hint == "allow":
        return RedirectResponse(target, status_code=302)
    if hint == "block":
        return _honeypot(request, cfg)
    # challenge
    return _challenge(request, cfg, token=token, target=target, bucket=bucket)


def _owner_bypass(cookie_secret: str) -> str:
    """Deterministic per-secret bypass token — reveal only in admin UI."""
    return hashlib.sha256(f"owner:{cookie_secret}".encode()).hexdigest()[:24]


def _honeypot(request: Request, cfg: dict) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, "honeypot.html", {
        "cfg": cfg,
    }, status_code=403)


def _challenge(request: Request, cfg: dict, *, token: str, target: str,
               bucket: str) -> HTMLResponse:
    """Serve the silent JS challenge page."""
    difficulty = int(cfg.get("pow_difficulty", "5"))
    pow_seed = hashlib.sha256(f"{bucket}|{int(time.time() // 60)}|{cfg.get('cookie_secret', '')}".encode()).hexdigest()[:16]
    return request.app.state.templates.TemplateResponse(request, "challenge.html", {
        "cfg": cfg,
        "token": token,
        "pow_seed": pow_seed,
        "pow_difficulty": difficulty,
        "target_preview": target,
    })


# ── verify: challenge form submits here ────────────────────

@router.post("/verify")
async def verify(request: Request,
                  token: str = Form(""),
                  honeypot: str = Form(""),
                  webdriver: str = Form(""),
                  webgl_vendor: str = Form(""),
                  canvas_hash: str = Form(""),
                  no_plugins: str = Form(""),
                  submit_ms: str = Form(""),
                  pow_seed: str = Form(""),
                  pow_answer: str = Form(""),
                  pow_hash: str = Form("")):
    db = request.app.state.db
    cfg = db.get_config()
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    hmac_secret = cfg.get("hmac_secret", "")
    cookie_secret = cfg.get("cookie_secret", "")

    payload = verify_token(hmac_secret, token) if hmac_secret else None
    target = (payload.get("t") if payload else "") or cfg.get("default_target", "")
    if not target:
        return PlainTextResponse("no target", status_code=400)

    bucket = session_bucket_from_request(ip, ua, cookie_secret or "salt")

    # PoW verification: sha256(seed:answer) must have >= difficulty leading '0' nibbles
    difficulty = int(cfg.get("pow_difficulty", "5"))
    pow_ok = False
    if pow_seed and pow_answer:
        h = hashlib.sha256(f"{pow_seed}:{pow_answer}".encode()).hexdigest()
        pow_ok = h.startswith("0" * difficulty) and (not pow_hash or h == pow_hash)
    client_signals = {
        "honeypot": bool(honeypot),
        "webdriver": webdriver == "true",
        "webgl_vendor": webgl_vendor,
        "canvas_hash": canvas_hash,
        "no_plugins": no_plugins == "true",
        "submit_ms": submit_ms,
        "pow_ok": pow_ok,
    }
    result = score_request(db, cfg, ip=ip, user_agent=ua,
                            client_signals=client_signals, rate_bucket=bucket)
    hint = result["verdict_hint"]
    dry_run = cfg.get("dry_run") == "1"

    dec_id = db.log_decision(
        ip=ip, asn=result["network"].get("asn", ""),
        country=result["network"].get("country", ""), user_agent=ua,
        target=target, verdict=hint, score=result["score"],
        signals_json=result["signals"],
        token_valid=1 if payload else 0,
        dry_run=1 if dry_run else 0)

    if result["score"] >= int(cfg.get("webhook_min_score", "70")):
        _fire_webhook(cfg, {"id": dec_id, "ip": ip, "score": result["score"],
                             "verdict": hint, "signals": result["signals"],
                             "phase": "verify", "ts": int(time.time())})

    if dry_run or hint == "allow":
        ttl = int(cfg.get("verification_ttl_hours", "6")) * 3600
        resp = RedirectResponse(target, status_code=302)
        resp.set_cookie(VERIFY_COOKIE,
                        issue_verify_cookie(cookie_secret, bucket, ttl),
                        max_age=ttl, httponly=True, samesite="strict",
                        secure=True, path="/")
        return resp
    if hint == "challenge":
        # a repeat challenge is suspicious — bump to block on retry
        return _honeypot(request, cfg)
    return _honeypot(request, cfg)


# ── /api/check — for external integration (POST) ──────────

@router.post("/api/check")
async def api_check(request: Request):
    """Programmatic check. POST JSON: {ip, user_agent, headers, client_signals}.
    Returns {verdict, score, signals}. Requires HMAC secret in X-Antibot-Auth
    header (constant-time compare)."""
    db = request.app.state.db
    cfg = db.get_config()
    import hmac as _hmac
    given = request.headers.get("x-antibot-auth", "")
    if not given or not _hmac.compare_digest(given, cfg.get("hmac_secret", "")):
        return PlainTextResponse("forbidden", status_code=403)
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("bad json", status_code=400)
    ip = body.get("ip", "")
    ua = body.get("user_agent", "")
    client_signals = body.get("client_signals") or {}
    bucket = session_bucket_from_request(ip, ua, cfg.get("cookie_secret", ""))
    result = score_request(db, cfg, ip=ip, user_agent=ua,
                            client_signals=client_signals, rate_bucket=bucket)
    return {
        "score": result["score"],
        "verdict": result["verdict_hint"],
        "signals": result["signals"],
        "network": {
            "asn": result["network"].get("asn", ""),
            "country": result["network"].get("country", ""),
        },
    }
