"""Porkbun Registrar Integration — https://porkbun.com/api/json/v3/documentation

Auth: apikey + secretapikey pro Request-Body (nicht Header).
Alle Requests sind POST mit JSON. Endpoints:
  /ping                           auth-test, returns your IP
  /domain/listAll?start=N         list domains (max 1000 per call, paginated)
  /domain/updateNs/{domain}       set nameservers (body: {ns: [...]})
  /domain/getNs/{domain}          get current NS
  /domain/checkDomain/{domain}    availability check

Wie Spaceship mit Retry-Semantik beim NS-Setzen für frisch registrierte Domains.
"""
import time
import logging
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()
logger = logging.getLogger("bulk.porkbun")

PORKBUN_BASE = "https://api.porkbun.com/api/json/v3"


# ── Low-Level API Call ────────────────────────────────────

def _pb_call(api_key: str, api_secret: str, path: str,
              body: dict = None, timeout: int = 20) -> dict:
    """Alle Porkbun-Calls sind POST mit apikey+secretapikey im Body."""
    import requests
    if not api_key or not api_secret:
        return {"_error": "Kein Porkbun API-Key + Secret gesetzt", "status": "ERROR"}
    payload = {"apikey": api_key, "secretapikey": api_secret}
    if body:
        payload.update(body)
    try:
        r = requests.post(f"{PORKBUN_BASE}{path}", json=payload, timeout=timeout)
        try:
            data = r.json() if r.text else {}
        except Exception:
            data = {"_raw": r.text[:500]}
        data.setdefault("_status", r.status_code)
        # Porkbun-Convention: "status" == "SUCCESS" oder "ERROR" + message
        if data.get("status") == "ERROR" and "_error" not in data:
            data["_error"] = data.get("message", "unknown error")
        return data
    except Exception as e:
        return {"_error": f"Porkbun-Request failed: {e}", "_status": 0, "status": "ERROR"}


def _pb_from_account_id(db, aid: int) -> tuple:
    row = db.get_porkbun_account(aid) if aid else db.get_primary_porkbun_account()
    if not row:
        return "", "", ""
    r = dict(row)
    return r.get("api_key", ""), r.get("api_secret", ""), r.get("name", "")


# ── High-Level Convenience ────────────────────────────────

def pb_ping(api_key: str, api_secret: str) -> dict:
    """Auth-Test — returns your IP address on success."""
    return _pb_call(api_key, api_secret, "/ping")


def pb_list_domains(api_key: str, api_secret: str, start: int = 0) -> dict:
    """List domains — bis 1000 pro Call, paginierbar via start=N."""
    return _pb_call(api_key, api_secret, "/domain/listAll",
                     body={"start": start, "includeLabels": "yes"})


def pb_get_ns(api_key: str, api_secret: str, domain: str) -> dict:
    return _pb_call(api_key, api_secret, f"/domain/getNs/{domain}")


def pb_check_availability(api_key: str, api_secret: str, domain: str) -> dict:
    """Rückgabe: {available: bool|None, price: str, currency: str,
                  premium: bool, raw: dict, error: str}."""
    resp = _pb_call(api_key, api_secret, f"/domain/checkDomain/{domain}")
    if resp.get("status") != "SUCCESS":
        return {
            "available": None, "price": "", "currency": "USD",
            "premium": False, "raw": resp,
            "error": resp.get("message") or resp.get("_error") or "unknown",
        }
    r = resp.get("response") or {}
    avail_raw = r.get("avail") or r.get("available")
    if isinstance(avail_raw, str):
        available = avail_raw.lower() in ("yes", "true", "available", "1")
    elif isinstance(avail_raw, bool):
        available = avail_raw
    else:
        available = None
    price = str(r.get("price") or r.get("regular_price") or "")
    return {
        "available": available, "price": price, "currency": "USD",
        "premium": bool(r.get("premium")),
        "raw": resp, "error": "",
    }


def pb_get_pricing(api_key: str, api_secret: str) -> dict:
    return _pb_call(api_key, api_secret, "/pricing/get")


def pb_set_nameservers(api_key: str, api_secret: str, domain: str,
                         ns_list: list) -> dict:
    """NS setzen — Body {ns: [...]}. Returns {ok, msg, not_ready_yet}."""
    if not ns_list or len(ns_list) < 2:
        return {"ok": False, "msg": "brauche mindestens 2 Nameserver",
                "not_ready_yet": False}
    resp = _pb_call(api_key, api_secret, f"/domain/updateNs/{domain}",
                    body={"ns": ns_list[:8]})   # Porkbun max 8 NS
    status = (resp.get("status") or "").upper()
    msg = resp.get("message") or resp.get("_error") or ""
    msg_lower = msg.lower()
    not_ready = ("not currently registered" in msg_lower
                 or "pending" in msg_lower
                 or "waiting" in msg_lower
                 or "not found" in msg_lower)
    if status == "SUCCESS":
        return {"ok": True, "msg": "NS gesetzt", "not_ready_yet": False}
    if not_ready:
        return {"ok": False, "msg": msg or "pending", "not_ready_yet": True}
    return {"ok": False, "msg": msg or f"HTTP {resp.get('_status')}",
            "not_ready_yet": False}


def pb_set_ns_with_retry(api_key: str, api_secret: str, domain: str,
                           ns_list: list, log_step=None) -> dict:
    is_de = domain.lower().endswith(".de")
    max_retries = 4 if is_de else 2
    wait_s = 30 if is_de else 10
    last = {"ok": False, "msg": "kein Versuch"}
    for attempt in range(max_retries):
        if attempt > 0:
            if log_step:
                log_step(f"NS: warte {wait_s}s (Attempt {attempt+1}/{max_retries})",
                         True, "Porkbun braucht kurz bis der Domain-Datensatz settled ist")
            time.sleep(wait_s)
        r = pb_set_nameservers(api_key, api_secret, domain, ns_list)
        last = r
        if r["ok"] or not r.get("not_ready_yet"):
            return r
    return last


# ── Panel Routes ──────────────────────────────────────────

@router.get("/porkbun", response_class=HTMLResponse)
async def porkbun_page(request: Request):
    db = request.app.state.db
    accounts = [dict(a) for a in db.get_porkbun_accounts()]
    primary = db.get_primary_porkbun_account()
    domains = []
    ping_ip = ""
    if primary:
        pd = dict(primary)
        p = pb_ping(pd["api_key"], pd["api_secret"])
        if p.get("_error"):
            ping_ip = f"— ({p['_error']})"
        else:
            ping_ip = p.get("yourIp") or p.get("your_ip") or "OK"
        dlist = pb_list_domains(pd["api_key"], pd["api_secret"])
        if dlist.get("status") == "SUCCESS":
            domains = dlist.get("domains") or []
    cf_accounts = [dict(a) for a in db.get_cf_accounts()]
    return request.app.state.templates.TemplateResponse(request, "porkbun.html", {
        "active": "porkbun",
        "accounts": accounts,
        "primary": primary and dict(primary),
        "domains": domains[:200],
        "ping_ip": ping_ip,
        "cf_accounts": cf_accounts,
    })


@router.post("/porkbun/accounts/add")
async def add_account(request: Request,
                       name: str = Form(""),
                       api_key: str = Form(""),
                       api_secret: str = Form("")):
    db = request.app.state.db
    if name.strip() and api_key.strip() and api_secret.strip():
        db.add_porkbun_account(name.strip(), api_key.strip(), api_secret.strip())
    return RedirectResponse("/porkbun", status_code=303)


@router.post("/porkbun/accounts/{aid}/update")
async def update_account(request: Request, aid: int,
                          name: str = Form(""),
                          api_key: str = Form(""),
                          api_secret: str = Form("")):
    db = request.app.state.db
    fields = {}
    if name.strip():
        fields["name"] = name.strip()
    if api_key.strip():
        fields["api_key"] = api_key.strip()
    if api_secret.strip():
        fields["api_secret"] = api_secret.strip()
    db.update_porkbun_account(aid, **fields)
    return RedirectResponse("/porkbun", status_code=303)


@router.post("/porkbun/accounts/{aid}/set-primary")
async def set_primary(request: Request, aid: int):
    request.app.state.db.set_primary_porkbun_account(aid)
    return RedirectResponse("/porkbun", status_code=303)


@router.post("/porkbun/accounts/{aid}/delete")
async def delete_account(request: Request, aid: int):
    request.app.state.db.delete_porkbun_account(aid)
    return RedirectResponse("/porkbun", status_code=303)


@router.post("/porkbun/test", response_class=HTMLResponse)
async def test_account(request: Request, account_id: int = Form(0)):
    db = request.app.state.db
    key, secret, name = _pb_from_account_id(db, account_id)
    if not key or not secret:
        return HTMLResponse('<span style="color:var(--red)">Kein Account oder API-Key fehlt</span>')
    p = pb_ping(key, secret)
    if p.get("_error"):
        return HTMLResponse(
            f'<span style="color:var(--red)">✗ {escape(p["_error"])}</span>'
        )
    return HTMLResponse(
        f'<span style="color:var(--green)">✓ Auth OK — dein IP bei Porkbun: '
        f'<code>{escape(str(p.get("yourIp", "?")))}</code></span>'
    )


@router.post("/porkbun/set-ns", response_class=HTMLResponse)
async def set_ns_route(request: Request, domain: str = Form(""),
                        nameservers: str = Form(""),
                        account_id: int = Form(0)):
    db = request.app.state.db
    key, secret, name = _pb_from_account_id(db, account_id)
    if not key or not secret:
        return HTMLResponse('<span style="color:var(--red)">Kein Account gesetzt</span>')
    domain = domain.strip().lower()
    ns = [n.strip() for n in nameservers.replace(",", "\n").splitlines() if n.strip()]
    if not domain or len(ns) < 2:
        return HTMLResponse('<span style="color:var(--red)">Domain + mind. 2 NS pflicht</span>')
    logs = []
    res = pb_set_ns_with_retry(
        key, secret, domain, ns,
        log_step=lambda label, ok, detail: logs.append((label, ok, detail)))
    log_html = "".join(
        f'<li>{"✓" if ok else "…"} {escape(label)}'
        f'{": " + escape(detail) if detail else ""}</li>'
        for label, ok, detail in logs
    )
    if res["ok"]:
        return HTMLResponse(
            f'<div style="color:var(--green)">✓ NS gesetzt für <code>{escape(domain)}</code>: '
            f'{escape(", ".join(ns))}</div>'
            f'<ul style="font-size:11px;color:var(--fg2)">{log_html}</ul>'
        )
    return HTMLResponse(
        f'<div style="color:var(--red)">✗ {escape(res["msg"])}</div>'
        f'<ul style="font-size:11px;color:var(--fg2)">{log_html}</ul>'
    )


@router.post("/porkbun/search", response_class=HTMLResponse)
async def search_domains(request: Request, query: str = Form(""),
                          account_id: int = Form(0)):
    """Bulk-Availability. Porkbun bietet KEIN API-Register — daher für
    verfügbare Domains ein Deep-Link zu Porkbun-Cart."""
    db = request.app.state.db
    key, secret, name = _pb_from_account_id(db, account_id)
    if not key or not secret:
        return HTMLResponse('<div class="alert alert-danger">Kein Porkbun-Account gesetzt.</div>')
    domains_raw = [d.strip().lower() for d in query.replace(",", "\n").splitlines() if d.strip()]
    if not domains_raw:
        return HTMLResponse('<div class="alert alert-warning">Mindestens eine Domain eingeben.</div>')

    # Domains im Account? (aus /domain/listAll)
    owned = set()
    try:
        dl = pb_list_domains(key, secret)
        if dl.get("status") == "SUCCESS":
            for it in dl.get("domains") or []:
                nm = (it.get("domain") or it.get("name") or "").lower()
                if nm:
                    owned.add(nm)
    except Exception as e:
        logger.warning("owned-lookup failed: %s", e)

    rows = ""
    for d in domains_raw[:40]:
        did = d.replace(".", "-")
        if d in owned:
            rows += (
                f'<tr>'
                f'<td style="font-family:monospace">{escape(d)}</td>'
                f'<td><span class="badge badge-info">In deinem Account</span></td>'
                f'<td>—</td>'
                f'<td>'
                f'<button class="btn btn-primary btn-xs" '
                f'hx-post="/porkbun/quick-cf" '
                f'hx-vals=\'{{"domain":"{escape(d)}"}}\' '
                f'hx-include="#pb-cf-account,#pb-search-account" '
                f'hx-target="#pb-search-result-{did}" hx-swap="innerHTML">'
                f'→ CF-Zone + NS setzen</button>'
                f'<div id="pb-search-result-{did}" style="margin-top:4px;font-size:11px"></div>'
                f'</td>'
                f'</tr>'
            )
            continue
        r = pb_check_availability(key, secret, d)
        if r["available"] is True:
            badge = '<span class="badge badge-running">Verfügbar</span>'
            if r["premium"]:
                badge = '<span class="badge badge-warning">Premium</span>'
            price = f'{r["price"]} {r["currency"]}' if r["price"] else '—'
            buy_url = f'https://porkbun.com/checkout/search?q={d}'
            action = (
                f'<a href="{escape(buy_url)}" target="_blank" rel="noopener" '
                f'class="btn btn-primary btn-xs">→ Bei Porkbun kaufen</a>'
                f'<span class="muted" style="margin-left:6px;font-size:11px">'
                f'nach Kauf zurück → „In deinem Account" → CF setzen</span>'
            )
        elif r["available"] is False:
            badge = '<span class="badge badge-failed">Vergeben</span>'
            price = "—"
            action = '<span class="muted" style="font-size:11px">—</span>'
        else:
            badge = '<span class="badge badge-warning">?</span>'
            price = "—"
            action = (
                f'<span class="muted" style="font-size:11px">'
                f'{escape(r["error"][:80])}</span>'
            )
        rows += (
            f'<tr>'
            f'<td style="font-family:monospace">{escape(d)}</td>'
            f'<td>{badge}</td>'
            f'<td style="font-size:12px">{escape(price)}</td>'
            f'<td>{action}</td>'
            f'</tr>'
        )

    return HTMLResponse(
        '<table style="font-size:12px"><thead><tr>'
        '<th>Domain</th><th>Status</th><th>Preis</th><th></th>'
        '</tr></thead><tbody>' + rows + '</tbody></table>'
        '<p class="muted" style="font-size:11px;margin-top:6px">'
        'Porkbun bietet kein Register-via-API — daher externer Buy-Link.</p>'
    )


@router.post("/porkbun/quick-cf", response_class=HTMLResponse)
async def porkbun_quick_cf(request: Request, domain: str = Form(""),
                            cf_account_id: int = Form(0),
                            account_id: int = Form(0)):
    return await set_ns_from_cf(request, domain=domain,
                                 cf_account_id=cf_account_id,
                                 account_id=account_id)


@router.post("/porkbun/set-ns-cf", response_class=HTMLResponse)
async def set_ns_from_cf(request: Request, domain: str = Form(""),
                          cf_account_id: int = Form(0),
                          account_id: int = Form(0)):
    """Auto-Flow: CF-Zone anlegen falls nicht da → NS bei Porkbun setzen mit Retry."""
    db = request.app.state.db
    key, secret, pb_name = _pb_from_account_id(db, account_id)
    if not key or not secret:
        return HTMLResponse('<div class="alert alert-danger">Kein Porkbun-Account gesetzt.</div>')
    domain = domain.strip().lower()
    if not domain or "." not in domain:
        return HTMLResponse('<div class="alert alert-warning">Ungültige Domain.</div>')

    from .dynadot import _cf_headers_for
    headers, cf_account = _cf_headers_for(db, cf_account_id)
    if not headers:
        return HTMLResponse('<div class="alert alert-danger">Kein Cloudflare-Account im Panel — erst dort hinterlegen.</div>')

    import requests as _req
    zone_id = None
    ns_list = []
    try:
        r = _req.get("https://api.cloudflare.com/client/v4/zones",
                     headers=headers, params={"name": domain}, timeout=15)
        data = r.json()
        if data.get("success") and data.get("result"):
            zone_id = data["result"][0]["id"]
            ns_list = data["result"][0].get("name_servers", [])
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">CF-Zone-Lookup failed: {escape(str(e))}</div>')

    ns_hint = ""
    if not zone_id:
        try:
            body = {"name": domain, "type": "full"}
            if cf_account:
                body["account"] = {"id": cf_account}
            r = _req.post("https://api.cloudflare.com/client/v4/zones",
                          headers=headers, json=body, timeout=20)
            data = r.json()
            if not data.get("success"):
                errs = "; ".join(e.get("message", "") for e in data.get("errors", []))
                return HTMLResponse(f'<div class="alert alert-danger">CF-Zone anlegen: {escape(errs)}</div>')
            zone_id = data["result"]["id"]
            ns_list = data["result"].get("name_servers", [])
            ns_hint = f"Neue CF-Zone angelegt (ID {zone_id[:12]}…)"
        except Exception as e:
            return HTMLResponse(f'<div class="alert alert-danger">CF Zone add failed: {escape(str(e))}</div>')

    if not ns_list:
        return HTMLResponse('<div class="alert alert-warning">CF hat keine Nameserver geliefert.</div>')

    logs = []
    if ns_hint:
        logs.append((ns_hint, True, ""))
    res = pb_set_ns_with_retry(
        key, secret, domain, ns_list,
        log_step=lambda label, ok, detail: logs.append((label, ok, detail)))
    log_html = "".join(
        f'<li>{"✓" if ok else "…"} <strong>{escape(label)}</strong>'
        f'{": " + escape(detail) if detail else ""}</li>'
        for label, ok, detail in logs
    )
    if res["ok"]:
        return HTMLResponse(
            f'<div class="alert alert-success">✓ <code>{escape(domain)}</code> zeigt jetzt auf Cloudflare</div>'
            f'<p class="muted" style="font-size:12px">NS: {escape(", ".join(ns_list))}</p>'
            f'<ul style="font-size:12px">{log_html}</ul>'
        )
    return HTMLResponse(
        f'<div class="alert alert-warning">NS-Set fehlgeschlagen: {escape(res["msg"])}</div>'
        f'<p class="muted" style="font-size:12px">CF-NS zum manuellen Setzen: <code>{escape(", ".join(ns_list))}</code></p>'
        f'<ul style="font-size:12px">{log_html}</ul>'
    )
