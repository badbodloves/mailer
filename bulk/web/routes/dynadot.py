"""Dynadot — Domain search, purchase, auto CF zone + NS setup."""
import time
import threading
import json
from html import escape
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

DYNADOT_BASE = "https://api.dynadot.com/api3.json"


def _dynadot_call(api_key: str, command: str, params: dict = None) -> dict:
    import requests
    url = f"{DYNADOT_BASE}?key={api_key}&command={command}"
    if params:
        for k, v in params.items():
            url += f"&{k}={v}"
    resp = requests.get(url, timeout=20)
    raw = resp.json()
    return raw.get("response", raw)


def _cf_headers_for(db, cf_account_id: int = 0) -> tuple:
    """Get CF auth headers + account_id for a specific or first CF account."""
    if cf_account_id:
        row = db._conn().execute("SELECT * FROM cf_accounts WHERE id=?", (cf_account_id,)).fetchone()
        accounts = [row] if row else []
    else:
        accounts = db.get_cf_accounts()
    if not accounts:
        return None, None
    acct = dict(accounts[0])
    account_id = acct.get("account_id", "")
    if acct.get("global_api_key") and acct.get("auth_email"):
        headers = {"X-Auth-Key": acct["global_api_key"],
                   "X-Auth-Email": acct["auth_email"],
                   "Content-Type": "application/json"}
    else:
        headers = {"Authorization": f"Bearer {acct.get('api_token', '')}",
                   "Content-Type": "application/json"}
    return headers, account_id


@router.get("/domains", response_class=HTMLResponse)
async def domains_page(request: Request):
    db = request.app.state.db
    tpl = request.app.state.templates
    config = db.get_dynadot_config()
    purchased = [dict(d) for d in db.get_purchased_domains()]
    cf_accounts = [dict(a) for a in db.get_cf_accounts()]
    return tpl.TemplateResponse(request, "domains.html", {
        "active": "domains", "config": config, "purchased": purchased,
        "cf_accounts": cf_accounts, "db": db,
    })


@router.post("/domains/config")
async def save_config(request: Request,
                      api_key: str = Form(""),
                      secret: str = Form("")):
    request.app.state.db.save_dynadot_config(api_key.strip(), secret.strip())
    return RedirectResponse("/domains", status_code=303)


@router.post("/domains/search", response_class=HTMLResponse)
async def search_domains(request: Request, query: str = Form("")):
    db = request.app.state.db
    config = db.get_dynadot_config()
    if not config.get("api_key"):
        return HTMLResponse('<div class="alert alert-danger">Dynadot API key not configured.</div>')

    domains_raw = [d.strip() for d in query.replace(",", "\n").splitlines() if d.strip()]
    if not domains_raw:
        return HTMLResponse('<div class="alert alert-warning">Enter at least one domain.</div>')

    params = {"show_price": "1", "currency": "EUR"}
    for i, d in enumerate(domains_raw[:50]):
        params[f"domain{i}"] = d

    try:
        data = _dynadot_call(config["api_key"], "search", params)
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">API error: {escape(str(e))}</div>')

    results = []
    search_results = data.get("SearchResults", data.get("search_results", data))
    if isinstance(search_results, dict):
        items = search_results.get("SearchResult", search_results.get("domain_info", []))
    elif isinstance(search_results, list):
        items = search_results
    else:
        items = []
    if not isinstance(items, list):
        items = [items]

    for item in items:
        name = item.get("DomainName", item.get("domain", item.get("Name", "")))
        status_val = item.get("Available", item.get("status", item.get("Status", "")))
        available = str(status_val).lower() in ("yes", "available", "true")
        price = item.get("Price", item.get("price", ""))
        results.append({"domain": name, "available": available, "price": price})

    if not results:
        return HTMLResponse('<div class="alert alert-info">No results returned. Check domain format.</div>')

    rows = ""
    for r in results:
        badge = "badge-running" if r["available"] else "badge-failed"
        label = "Available" if r["available"] else "Taken"
        price_str = f'{r["price"]} EUR' if r["price"] and r["available"] else "—"
        buy_btn = ""
        if r["available"]:
            buy_btn = (f'<button class="btn btn-primary btn-xs" '
                       f'hx-post="/domains/buy" '
                       f'hx-vals=\'js:{{domain:"{r["domain"]}", '
                       f'cf_account_id: document.getElementById("cf-account-select")?.value || "0"}}\' '
                       f'hx-target="#buy-result" hx-swap="innerHTML" '
                       f'hx-confirm="Buy {r["domain"]} for {price_str}?">Buy</button>')
        rows += (f'<tr><td style="font-weight:500">{escape(r["domain"])}</td>'
                 f'<td><span class="badge {badge}">{label}</span></td>'
                 f'<td>{price_str}</td><td>{buy_btn}</td></tr>')

    return HTMLResponse(
        f'<table><thead><tr><th>Domain</th><th>Status</th><th>Price</th><th></th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
    )


@router.post("/domains/balance", response_class=HTMLResponse)
async def check_balance(request: Request):
    db = request.app.state.db
    config = db.get_dynadot_config()
    if not config.get("api_key"):
        return HTMLResponse('<span style="color:var(--red)">No API key</span>')
    try:
        data = _dynadot_call(config["api_key"], "get_account_balance")
        bal_resp = data.get("GetAccountBalanceResponse", data)
        balance = bal_resp.get("AccountBalance", bal_resp.get("account_balance", bal_resp))
        if isinstance(balance, dict):
            amt = balance.get("Balance", balance.get("balance", "?"))
            cur = balance.get("Currency", balance.get("currency", "USD"))
        else:
            amt = balance
            cur = "USD"
        return HTMLResponse(
            f'<span style="color:var(--green);font-weight:600">{amt} {cur}</span>')
    except Exception as e:
        return HTMLResponse(f'<span style="color:var(--red)">{escape(str(e))}</span>')


@router.post("/domains/buy", response_class=HTMLResponse)
async def buy_domain(request: Request, domain: str = Form(""),
                     cf_account_id: int = Form(0)):
    db = request.app.state.db
    config = db.get_dynadot_config()
    if not config.get("api_key") or not domain.strip():
        return HTMLResponse('<div class="alert alert-danger">Missing API key or domain.</div>')

    domain = domain.strip().lower()
    log = []

    # Step 0: Check availability first
    log.append(f"Checking availability of {domain}...")
    try:
        check = _dynadot_call(config["api_key"], "search", {"domain0": domain, "show_price": "1", "currency": "EUR"})
        sr = check.get("SearchResults", check.get("search_results", {}))
        items = sr.get("SearchResult", sr.get("domain_info", []))
        if not isinstance(items, list):
            items = [items]
        if items:
            status_val = str(items[0].get("Available", items[0].get("status", ""))).lower()
            if status_val not in ("yes", "available", "true"):
                log.append(f"Domain {domain} is NOT available ({status_val}).")
                return HTMLResponse(_fmt_log(log))
            price = items[0].get("Price", items[0].get("price", "?"))
            log.append(f"Available! Price: {price} EUR")
        else:
            log.append("Could not verify availability, proceeding anyway...")
    except Exception as e:
        log.append(f"Availability check error: {e}, proceeding...")

    # Step 1: Register
    log.append(f"Registering {domain}...")
    try:
        data = _dynadot_call(config["api_key"], "register", {"domain": domain, "duration": "1"})
        reg_resp = data.get("DomainRegistrationResponse", data.get("RegisterResponse", data))
        error_str = str(reg_resp).lower()
        if "error" in error_str or "not available" in error_str or "insufficient" in error_str:
            errors = reg_resp.get("Error", reg_resp.get("errors", str(reg_resp)))
            log.append(f"Registration failed: {errors}")
            return HTMLResponse(_fmt_log(log))
        log.append("Registration successful!")
    except Exception as e:
        log.append(f"Registration error: {e}")
        return HTMLResponse(_fmt_log(log))

    # Step 2: Create CF Zone
    cf_headers, account_id = _cf_headers_for(db, cf_account_id)
    zone_id = ""
    ns1 = ""
    ns2 = ""

    if cf_headers and account_id:
        log.append("Creating Cloudflare zone...")
        try:
            import requests
            resp = requests.post(
                "https://api.cloudflare.com/client/v4/zones",
                headers=cf_headers,
                json={"name": domain, "account": {"id": account_id}, "jump_start": False},
                timeout=15)
            zdata = resp.json()
            if zdata.get("success"):
                result = zdata["result"]
                zone_id = result.get("id", "")
                ns_list = result.get("name_servers", [])
                ns1 = ns_list[0] if len(ns_list) > 0 else ""
                ns2 = ns_list[1] if len(ns_list) > 1 else ""
                log.append(f"Zone created: {zone_id[:12]}... NS: {ns1}, {ns2}")
            else:
                errs = zdata.get("errors", [])
                log.append(f"CF zone warning: {errs}")
        except Exception as e:
            log.append(f"CF zone error: {e}")
    else:
        log.append("No Cloudflare account — skipping zone setup.")

    # Step 3: Save to DB
    db.add_purchased_domain(domain, zone_id, ns1, ns2)
    log.append("Domain saved to database.")

    # Step 4: Set nameservers (with retry for .de)
    if ns1 and ns2:
        log.append("Setting nameservers...")
        is_de = domain.endswith(".de")
        max_retries = 4 if is_de else 2

        for attempt in range(max_retries):
            if attempt > 0:
                wait = 30 if is_de else 10
                log.append(f"Waiting {wait}s for DNS propagation (attempt {attempt+1})...")
                time.sleep(wait)
            try:
                data = _dynadot_call(config["api_key"], "set_ns", {
                    "domain": domain, "ns1": ns1, "ns2": ns2})
                ns_resp = data.get("SetNsResponse", data)
                ns_str = str(ns_resp).lower()
                if "dns queries" in ns_str or "must respond" in ns_str:
                    log.append(f"NS not ready yet (DNS queries check).")
                    continue
                elif "error" in ns_str:
                    log.append(f"NS error: {ns_resp}")
                    continue
                else:
                    db.update_purchased_domain(domain, ns_set=1)
                    log.append("Nameservers set successfully!")
                    break
            except Exception as e:
                log.append(f"NS error: {e}")
        else:
            log.append("NS setting failed after retries. Set manually or retry later.")

    log.append(f"Done! {domain} is ready.")
    return HTMLResponse(_fmt_log(log))


@router.post("/domains/{did}/set-ns", response_class=HTMLResponse)
async def retry_set_ns(request: Request, did: int):
    """Retry setting nameservers for a purchased domain."""
    db = request.app.state.db
    row = db._conn().execute("SELECT * FROM purchased_domains WHERE id=?", (did,)).fetchone()
    if not row:
        return HTMLResponse('<span style="color:var(--red)">Not found</span>')
    row = dict(row)
    config = db.get_dynadot_config()
    if not config.get("api_key"):
        return HTMLResponse('<span style="color:var(--red)">No API key</span>')

    ns1 = row.get("cf_ns1", "")
    ns2 = row.get("cf_ns2", "")
    if not ns1 or not ns2:
        return HTMLResponse('<span style="color:var(--fg2)">No nameservers — create CF zone first</span>')

    try:
        data = _dynadot_call(config["api_key"], "set_ns", {
            "domain": row["domain"], "ns1": ns1, "ns2": ns2})
        ns_resp = data.get("SetNsResponse", data)
        ns_str = str(ns_resp).lower()
        if "error" not in ns_str and "must respond" not in ns_str:
            db.update_purchased_domain(row["domain"], ns_set=1)
            return HTMLResponse('<span class="badge badge-running">NS set!</span>')
        else:
            return HTMLResponse(f'<span style="color:var(--red)">Failed: {escape(str(ns_resp)[:100])}</span>')
    except Exception as e:
        return HTMLResponse(f'<span style="color:var(--red)">{escape(str(e))}</span>')


@router.post("/domains/list-remote", response_class=HTMLResponse)
async def list_remote_domains(request: Request):
    """List all domains on Dynadot account."""
    db = request.app.state.db
    config = db.get_dynadot_config()
    if not config.get("api_key"):
        return HTMLResponse('<div class="alert alert-danger">No API key</div>')
    try:
        data = _dynadot_call(config["api_key"], "list_domain")
        list_resp = data.get("DomainListResponse", data)
        domains = list_resp.get("DomainInfoList", list_resp.get("domain_list_response", list_resp))
        if isinstance(domains, dict):
            items = domains.get("DomainInfo", domains.get("domains", []))
        else:
            items = domains if isinstance(domains, list) else []
        if not isinstance(items, list):
            items = [items]

        rows = ""
        for d in items:
            name = d.get("Name", d.get("name", ""))
            status = d.get("Status", d.get("status", ""))
            exp = d.get("Expiration", d.get("expiration", ""))
            rows += f'<tr><td>{escape(str(name))}</td><td>{escape(str(status))}</td><td style="font-size:12px">{escape(str(exp))}</td></tr>'

        return HTMLResponse(
            f'<table><thead><tr><th>Domain</th><th>Status</th><th>Expires</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            f'<p style="font-size:12px;color:var(--fg2);margin-top:6px">{len(items)} domains</p>')
    except Exception as e:
        return HTMLResponse(f'<div class="alert alert-danger">{escape(str(e))}</div>')


def _fmt_log(lines: list) -> str:
    html = '<div style="font-family:monospace;font-size:12px;background:var(--bg);padding:12px;border-radius:var(--radius)">'
    for line in lines:
        if "successful" in line.lower() or "done" in line.lower() or "set!" in line.lower():
            color = "var(--green)"
        elif "failed" in line.lower() or "error" in line.lower():
            color = "var(--red)"
        elif "waiting" in line.lower() or "warning" in line.lower():
            color = "var(--yellow)"
        else:
            color = "var(--fg)"
        html += f'<div style="color:{color};padding:2px 0">{escape(line)}</div>'
    html += '</div>'
    return html
