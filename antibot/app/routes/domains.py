"""Domain-Panel — Dynadot (Kauf + Search) + Cloudflare (Zones + DNS)
+ kombinierter Buy-and-Connect Workflow."""
import time
import logging
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("antibot.domains")
router = APIRouter()

DYNADOT_BASE = "https://api.dynadot.com/api3.json"
CF_BASE = "https://api.cloudflare.com/client/v4"


# ── Dynadot helpers ────────────────────────────────────────

def _dyn_call(api_key: str, command: str, params: dict = None,
              secret: str = "") -> dict:
    """Dynadot API v3 (JSON). Returns parsed dict, or {'error': msg}.
    `secret` is Dynadot's optional API-secret (needed for some actions
    like register / renew when the account has 'API secret' enabled)."""
    import requests
    if not api_key:
        return {"error": "Kein Dynadot API-Key gesetzt."}
    url = f"{DYNADOT_BASE}?key={api_key}&command={command}"
    if secret:
        url += f"&secret={secret}"
    if params:
        for k, v in params.items():
            url += f"&{k}={v}"
    try:
        r = requests.get(url, timeout=25)
        return r.json()
    except Exception as e:
        return {"error": f"Dynadot request failed: {e}"}


def _dyn_balance(api_key: str, secret: str = "") -> str:
    resp = _dyn_call(api_key, "account_info", secret=secret)
    if "error" in resp:
        return f"— ({resp['error']})"
    try:
        return resp.get("AccountInfoResponse", {}).get("Account", {}).get("AccountBalance", "?")
    except Exception:
        return "?"


def _dyn_search(api_key: str, domain: str, currency: str = "USD",
                 secret: str = "") -> dict:
    """Check availability + price for a single domain."""
    resp = _dyn_call(api_key, "search",
                     {"domain0": domain, "currency": currency}, secret=secret)
    if "error" in resp:
        return {"available": False, "price": "", "error": resp["error"]}
    try:
        r = resp["SearchResponse"]["SearchResults"][0]
        return {
            "available": r.get("Available") == "yes",
            "price": r.get("Price", ""),
            "raw": r,
            "error": "",
        }
    except Exception as e:
        return {"available": False, "price": "", "error": f"Parse: {e}", "raw": resp}


def _dyn_register(api_key: str, domain: str, currency: str = "USD",
                   duration: int = 1, secret: str = "") -> dict:
    """Actually buy the domain. Returns {'ok': bool, 'msg': str, 'raw': ...}."""
    params = {"domain": domain, "duration": duration, "currency": currency}
    resp = _dyn_call(api_key, "register", params, secret=secret)
    if "error" in resp:
        return {"ok": False, "msg": resp["error"]}
    try:
        r = resp["RegisterResponse"]
        status = (r.get("ResponseCode") or "").strip()
        if status in ("0", "success"):
            return {"ok": True, "msg": "gekauft", "raw": r}
        return {"ok": False, "msg": r.get("Status") or r.get("Error") or str(r), "raw": r}
    except Exception:
        return {"ok": False, "msg": "unklare Dynadot-Antwort", "raw": resp}


def _dyn_domains(api_key: str, secret: str = "") -> list:
    resp = _dyn_call(api_key, "list_domain", secret=secret)
    if "error" in resp:
        return []
    try:
        d = resp["ListDomainInfoResponse"]["MainDomains"]["Domain"]
        if isinstance(d, dict):
            d = [d]
        return d
    except Exception:
        return []


# ── Cloudflare helpers ────────────────────────────────────

def _cf_auth(cfg: dict) -> dict:
    """Prefer Global-API-Key + Email if both are set, else fall back to
    Bearer token. Returns headers dict (or empty dict if nothing configured)."""
    gk = (cfg.get("cloudflare_global_api_key") or "").strip()
    email = (cfg.get("cloudflare_auth_email") or "").strip()
    if gk and email:
        return {
            "X-Auth-Key": gk,
            "X-Auth-Email": email,
            "Content-Type": "application/json",
        }
    token = (cfg.get("cloudflare_api_token") or "").strip()
    if token:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
    return {}


def _cf_get(cfg: dict, path: str, params: dict = None) -> dict:
    import requests
    headers = _cf_auth(cfg)
    if not headers:
        return {"success": False, "errors": [{"message": "Weder CF-Token noch Global-Key/Email gesetzt."}]}
    try:
        r = requests.get(f"{CF_BASE}{path}", headers=headers,
                         params=params or {}, timeout=20)
        return r.json()
    except Exception as e:
        return {"success": False, "errors": [{"message": str(e)}]}


def _cf_post(cfg: dict, path: str, body: dict) -> dict:
    import requests
    headers = _cf_auth(cfg)
    if not headers:
        return {"success": False, "errors": [{"message": "Kein CF-Auth."}]}
    try:
        r = requests.post(f"{CF_BASE}{path}", headers=headers,
                          json=body, timeout=20)
        return r.json()
    except Exception as e:
        return {"success": False, "errors": [{"message": str(e)}]}


def _cf_delete(cfg: dict, path: str) -> dict:
    import requests
    headers = _cf_auth(cfg)
    if not headers:
        return {"success": False, "errors": [{"message": "Kein CF-Auth."}]}
    try:
        r = requests.delete(f"{CF_BASE}{path}", headers=headers, timeout=20)
        return r.json()
    except Exception as e:
        return {"success": False, "errors": [{"message": str(e)}]}


def _cf_zones(cfg: dict) -> list:
    resp = _cf_get(cfg, "/zones", {"per_page": 50})
    if not resp.get("success"):
        return []
    return resp.get("result", [])


def _cf_records(cfg: dict, zone_id: str) -> list:
    resp = _cf_get(cfg, f"/zones/{zone_id}/dns_records", {"per_page": 100})
    if not resp.get("success"):
        return []
    return resp.get("result", [])


def _cf_add_zone(cfg: dict, name: str, account_id: str) -> dict:
    body = {"name": name, "type": "full"}
    if account_id:
        body["account"] = {"id": account_id}
    return _cf_post(cfg, "/zones", body)


def _cf_add_record(cfg: dict, zone_id: str, rtype: str, name: str,
                    content: str, proxied: bool = False, ttl: int = 1) -> dict:
    return _cf_post(cfg, f"/zones/{zone_id}/dns_records", {
        "type": rtype, "name": name, "content": content,
        "proxied": proxied, "ttl": int(ttl),
    })


def _cf_delete_record(cfg: dict, zone_id: str, record_id: str) -> dict:
    return _cf_delete(cfg, f"/zones/{zone_id}/dns_records/{record_id}")


def _cf_configured(cfg: dict) -> bool:
    return bool(_cf_auth(cfg))


# ── Panel routes ──────────────────────────────────────────

@router.get("/admin/domains", response_class=HTMLResponse)
async def domains_page(request: Request, tab: str = "dynadot"):
    db = request.app.state.db
    cfg = db.get_config()

    dyn_balance = ""
    dyn_domains = []
    if cfg.get("dynadot_api_key"):
        dyn_balance = _dyn_balance(cfg["dynadot_api_key"], cfg.get("dynadot_api_secret", ""))
        dyn_domains = _dyn_domains(cfg["dynadot_api_key"], cfg.get("dynadot_api_secret", ""))

    cf_zones = []
    if _cf_configured(cfg):
        cf_zones = _cf_zones(cfg)

    return request.app.state.templates.TemplateResponse(request, "admin_domains.html", {
        "cfg": cfg,
        "tab": tab if tab in ("dynadot", "cloudflare", "combined") else "dynadot",
        "dyn_balance": dyn_balance,
        "dyn_domains": dyn_domains,
        "cf_zones": cf_zones,
    })


@router.post("/admin/domains/dynadot/save")
async def dyn_save(request: Request, api_key: str = Form(""),
                    api_secret: str = Form(""),
                    buy_currency: str = Form("USD")):
    request.app.state.db.set_config(
        dynadot_api_key=api_key.strip(),
        dynadot_api_secret=api_secret.strip(),
        buy_currency=buy_currency.strip() or "USD",
    )
    return RedirectResponse("/admin/domains?tab=dynadot", status_code=303)


@router.post("/admin/domains/dynadot/search", response_class=HTMLResponse)
async def dyn_search_route(request: Request, domains: str = Form("")):
    db = request.app.state.db
    cfg = db.get_config()
    lines = [d.strip().lower() for d in domains.replace(",", "\n").splitlines()
              if d.strip()]
    lines = lines[:20]
    if not cfg.get("dynadot_api_key"):
        return HTMLResponse('<div class="alert alert-danger">Kein Dynadot-Key gesetzt.</div>')
    rows_html = []
    for d in lines:
        r = _dyn_search(cfg["dynadot_api_key"], d, cfg.get("buy_currency", "USD"),
                        secret=cfg.get("dynadot_api_secret", ""))
        badge = ('<span class="v-allow">verfügbar</span>' if r["available"]
                 else '<span class="v-block">belegt</span>')
        price = escape(str(r.get("price", "")))
        err = f'<span class="muted">{escape(r["error"])}</span>' if r.get("error") else ""
        buy_form = ""
        if r["available"]:
            buy_form = (f'<form method="post" action="/admin/domains/dynadot/buy" style="display:inline" '
                        f'onsubmit="return confirm(\'Wirklich {escape(d)} kaufen für {price}?\');">'
                        f'<input type="hidden" name="domain" value="{escape(d)}">'
                        f'<button class="btn btn-success btn-xs">Kaufen</button>'
                        f'</form>')
        rows_html.append(f'<tr><td>{escape(d)}</td><td>{badge}</td>'
                         f'<td>{price}</td><td>{buy_form} {err}</td></tr>')
    return HTMLResponse(
        '<table style="font-size:12px"><thead><tr><th>Domain</th><th>Status</th>'
        '<th>Preis</th><th></th></tr></thead><tbody>' + "".join(rows_html)
        + '</tbody></table>'
    )


@router.post("/admin/domains/dynadot/buy", response_class=HTMLResponse)
async def dyn_buy(request: Request, domain: str = Form("")):
    db = request.app.state.db
    cfg = db.get_config()
    if not cfg.get("dynadot_api_key"):
        return HTMLResponse('<span class="v-block">Kein Dynadot-Key</span>')
    domain = domain.strip().lower()
    if not domain:
        return HTMLResponse('<span class="v-block">Empty</span>')
    res = _dyn_register(cfg["dynadot_api_key"], domain,
                        currency=cfg.get("buy_currency", "USD"),
                        secret=cfg.get("dynadot_api_secret", ""))
    if res["ok"]:
        return HTMLResponse(f'<span class="v-allow">✓ {escape(domain)} gekauft.</span> '
                            f'<a href="/admin/domains?tab=combined&connect={escape(domain)}" '
                            f'style="font-size:12px;margin-left:6px">→ Jetzt mit Cloudflare verbinden</a>')
    return HTMLResponse(f'<span class="v-block">✗ {escape(res["msg"])}</span>')


# ── Cloudflare panel actions ─────────────────────────────

@router.post("/admin/domains/cf/save")
async def cf_save(request: Request, api_token: str = Form(""),
                   global_api_key: str = Form(""),
                   auth_email: str = Form(""),
                   account_id: str = Form("")):
    request.app.state.db.set_config(
        cloudflare_api_token=api_token.strip(),
        cloudflare_global_api_key=global_api_key.strip(),
        cloudflare_auth_email=auth_email.strip(),
        cloudflare_account_id=account_id.strip(),
    )
    return RedirectResponse("/admin/domains?tab=cloudflare", status_code=303)


@router.get("/admin/domains/cf/zone/{zone_id}", response_class=HTMLResponse)
async def cf_zone_view(request: Request, zone_id: str):
    db = request.app.state.db
    cfg = db.get_config()
    if not _cf_configured(cfg):
        return HTMLResponse('<div class="alert alert-danger">Weder CF-Token noch Global-Key/Email gesetzt.</div>')
    records = _cf_records(cfg, zone_id)
    zones = _cf_zones(cfg)
    zone = next((z for z in zones if z["id"] == zone_id), None)
    zone_name = zone["name"] if zone else zone_id
    rows_html = []
    for r in records:
        rows_html.append(
            f'<tr><td>{escape(r.get("type", ""))}</td>'
            f'<td style="font-family:monospace;font-size:12px">{escape(r.get("name", ""))}</td>'
            f'<td style="font-family:monospace;font-size:12px">{escape(str(r.get("content", "")))}</td>'
            f'<td>{"proxied" if r.get("proxied") else "DNS only"}</td>'
            f'<td><form method="post" action="/admin/domains/cf/zone/{zone_id}/record/{r["id"]}/delete" '
            f'style="display:inline" onsubmit="return confirm(\'Record löschen?\');">'
            f'<button class="btn btn-danger btn-xs">×</button></form></td></tr>')
    return HTMLResponse(
        f'<h3>{escape(zone_name)}</h3>'
        f'<form method="post" action="/admin/domains/cf/zone/{zone_id}/record/add" '
        f'style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">'
        f'<select name="rtype"><option>A</option><option>AAAA</option>'
        f'<option>CNAME</option><option>TXT</option><option>MX</option></select>'
        f'<input name="name" placeholder="sub (oder @)" required style="width:120px">'
        f'<input name="content" placeholder="1.2.3.4 / target.example.com" required style="flex:1;min-width:200px">'
        f'<label style="display:flex;align-items:center;gap:4px">'
        f'<input type="checkbox" name="proxied" value="1"> proxied</label>'
        f'<button class="btn btn-primary btn-sm">Add</button></form>'
        f'<table><thead><tr><th>Type</th><th>Name</th><th>Content</th><th></th><th></th></tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody></table>'
    )


@router.post("/admin/domains/cf/zone/{zone_id}/record/add", response_class=HTMLResponse)
async def cf_record_add(request: Request, zone_id: str,
                         rtype: str = Form("A"), name: str = Form(""),
                         content: str = Form(""), proxied: str = Form("")):
    db = request.app.state.db
    cfg = db.get_config()
    resp = _cf_add_record(cfg, zone_id, rtype,
                           name.strip(), content.strip(), bool(proxied))
    if not resp.get("success"):
        errs = "; ".join(e.get("message", "") for e in resp.get("errors", []))
        return HTMLResponse(f'<div class="alert alert-danger">{escape(errs) or "Fehler"}</div>')
    return await cf_zone_view(request, zone_id)


@router.post("/admin/domains/cf/zone/{zone_id}/record/{rec_id}/delete", response_class=HTMLResponse)
async def cf_record_delete(request: Request, zone_id: str, rec_id: str):
    db = request.app.state.db
    cfg = db.get_config()
    _cf_delete(cfg, f"/zones/{zone_id}/dns_records/{rec_id}")
    return await cf_zone_view(request, zone_id)


@router.post("/admin/domains/cf/add-zone", response_class=HTMLResponse)
async def cf_add_zone_route(request: Request, name: str = Form("")):
    """Add an existing domain to CF (as a full zone)."""
    db = request.app.state.db
    cfg = db.get_config()
    name = name.strip().lower()
    if not name or "." not in name:
        return HTMLResponse('<div class="alert alert-warning">Gültige Domain angeben.</div>')
    resp = _cf_add_zone(cfg, name, cfg.get("cloudflare_account_id", ""))
    if not resp.get("success"):
        errs = "; ".join(e.get("message", "") for e in resp.get("errors", []))
        return HTMLResponse(f'<div class="alert alert-danger">{escape(errs) or "Fehler"}</div>')
    zone = resp.get("result", {})
    ns_list = zone.get("name_servers", [])
    ns_html = "<br>".join(f"<code>{escape(ns)}</code>" for ns in ns_list)
    return HTMLResponse(
        f'<div class="alert alert-success">Zone <code>{escape(name)}</code> angelegt. '
        f'Bei <strong>Dynadot</strong> die Nameserver auf folgende umstellen:<br>{ns_html}</div>')


# ── Combined: buy + auto-add CF zone + A-record ──────────

@router.get("/admin/domains/combined", response_class=HTMLResponse)
async def combined_view(request: Request):
    return RedirectResponse("/admin/domains?tab=combined", status_code=303)


@router.post("/admin/domains/combined/connect", response_class=HTMLResponse)
async def combined_connect(request: Request,
                            domain: str = Form(""),
                            ip: str = Form(""),
                            add_www: str = Form("1")):
    """Take an already-registered (or existing) domain, add it as CF zone,
    then create an A-record @ → ip (and optionally www)."""
    db = request.app.state.db
    cfg = db.get_config()
    if not _cf_configured(cfg):
        return HTMLResponse('<div class="alert alert-danger">Weder CF-Token noch Global-Key/Email gesetzt.</div>')
    domain = domain.strip().lower()
    ip = ip.strip()
    if not domain or not ip:
        return HTMLResponse('<div class="alert alert-warning">Domain und IP nötig.</div>')

    # Ist die Zone schon in CF? Sonst neu anlegen.
    zones = _cf_zones(cfg)
    zone = next((z for z in zones if z["name"] == domain), None)
    ns_hint = ""
    if not zone:
        resp = _cf_add_zone(cfg, domain, cfg.get("cloudflare_account_id", ""))
        if not resp.get("success"):
            errs = "; ".join(e.get("message", "") for e in resp.get("errors", []))
            return HTMLResponse(f'<div class="alert alert-danger">Zone add failed: {escape(errs)}</div>')
        zone = resp.get("result", {})
        ns_list = zone.get("name_servers", [])
        ns_hint = ('<div class="alert alert-warn">Neue CF-Zone angelegt. Bei Dynadot die '
                   'Nameserver umstellen auf:<br>'
                   + "<br>".join(f"<code>{escape(ns)}</code>" for ns in ns_list) + '</div>')

    zid = zone["id"]

    logs = []
    r1 = _cf_add_record(cfg, zid, "A", domain, ip, proxied=False)
    logs.append(("@ → " + ip, r1.get("success"), r1))
    if add_www:
        r2 = _cf_add_record(cfg, zid, "A", f"www.{domain}", ip, proxied=False)
        logs.append(("www → " + ip, r2.get("success"), r2))

    log_html = "".join(
        f'<li>{escape(label)}: '
        + ('<span class="v-allow">OK</span>' if ok else
           f'<span class="v-block">Fehler: {escape("; ".join(e.get("message","") for e in raw.get("errors", [])))}</span>')
        + '</li>'
        for label, ok, raw in logs
    )
    return HTMLResponse(
        f'{ns_hint}'
        f'<div class="alert alert-success">Verbunden: <code>{escape(domain)}</code> → <code>{escape(ip)}</code></div>'
        f'<ul style="font-size:13px">{log_html}</ul>'
        f'<p class="muted">DNS-Propagation kann bis 5 Min dauern. Danach kann Caddy auf dem Ziel-Server das Cert holen.</p>'
    )
