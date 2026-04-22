"""Cloudflare — Accounts + domain pulling."""
import threading
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

# Background pull results keyed by cf account id
_pull_results: dict = {}


@router.get("/cloudflare", response_class=HTMLResponse)
async def cloudflare_page(request: Request):
    db = request.app.state.db
    tpl = request.app.state.templates
    accounts = []
    for a in db.get_cf_accounts():
        ad = dict(a)
        ad["domains"] = _pull_results.get(a["id"], [])
        accounts.append(ad)
    return tpl.TemplateResponse(request, "cloudflare.html", {
        "active": "cloudflare", "accounts": accounts, "db": db,
    })


@router.post("/cloudflare/add-account")
async def add_account(request: Request,
                      name: str = Form(""),
                      auth_type: str = Form("token"),
                      api_token: str = Form(""),
                      global_api_key: str = Form(""),
                      auth_email: str = Form(""),
                      account_id: str = Form(""),
                      r2_access_key: str = Form(""),
                      r2_secret_key: str = Form("")):
    if not name.strip():
        return RedirectResponse("/cloudflare", status_code=303)
    request.app.state.db.add_cf_account(
        name=name.strip(),
        auth_type=auth_type,
        api_token=api_token.strip(),
        global_api_key=global_api_key.strip(),
        auth_email=auth_email.strip(),
        account_id=account_id.strip(),
        r2_access_key=r2_access_key.strip(),
        r2_secret_key=r2_secret_key.strip(),
    )
    return RedirectResponse("/cloudflare", status_code=303)


@router.post("/cloudflare/{cid}/delete")
async def delete_account(request: Request, cid: int):
    request.app.state.db.delete_cf_account(cid)
    _pull_results.pop(cid, None)
    return RedirectResponse("/cloudflare", status_code=303)


def _pull_domains_worker(db, cid: int):
    """Background worker that pulls zones from Cloudflare API."""
    import requests as req_lib
    acct = db._conn().execute("SELECT * FROM cf_accounts WHERE id=?", (cid,)).fetchone()
    if not acct:
        _pull_results[cid] = [{"name": "(account not found)", "status": "", "in_brands": False}]
        return
    acct = dict(acct)

    if acct.get("global_api_key") and acct.get("auth_email"):
        headers = {"X-Auth-Key": acct["global_api_key"],
                   "X-Auth-Email": acct["auth_email"]}
    else:
        headers = {"Authorization": f"Bearer {acct.get('api_token', '')}"}

    try:
        resp = req_lib.get("https://api.cloudflare.com/client/v4/zones",
                           headers=headers, params={"per_page": 50}, timeout=15)
        zones = resp.json().get("result", []) if resp.status_code == 200 else []
    except Exception:
        zones = []

    brand_domains = {d["domain"] for d in db.get_domains()}
    results = []
    for z in zones:
        results.append({
            "name": z.get("name", ""),
            "status": z.get("status", ""),
            "in_brands": z.get("name", "") in brand_domains,
        })
    _pull_results[cid] = results


@router.post("/cloudflare/{cid}/pull-domains", response_class=HTMLResponse)
async def pull_domains(request: Request, cid: int):
    """Start background domain pull; return HTMX-compatible domain list."""
    db = request.app.state.db
    _pull_results[cid] = [{"name": "Pulling...", "status": "loading", "in_brands": False}]

    t = threading.Thread(target=_pull_domains_worker, args=(db, cid), daemon=True)
    t.start()
    t.join(timeout=20)

    domains = _pull_results.get(cid, [])
    rows = ""
    for d in domains:
        brand_badge = '<span class="badge badge-running">Brands</span>' if d.get("in_brands") else ""
        status = d.get("status", "")
        rows += (f'<tr><td>{d["name"]}</td>'
                 f'<td>{status}</td>'
                 f'<td>{brand_badge}</td></tr>')

    return HTMLResponse(
        f'<table><thead><tr><th>Domain</th><th>Status</th><th>In Brands</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        f'<p style="font-size:12px;color:var(--fg2);margin-top:8px">{len(domains)} domains found</p>'
    )
