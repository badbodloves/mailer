"""Brands + Domains page."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

@router.get("/brands", response_class=HTMLResponse)
async def brands_page(request: Request):
    db = request.app.state.db
    tpl = request.app.state.templates
    brands = db.get_brands()
    brand_data = []
    for b in brands:
        bd = dict(b)
        bd["domains"] = [dict(d) for d in db.get_domains(b["id"])]
        bd["used"] = len(db.get_used_lists(b["id"]))
        bd["unused"] = len(db.get_unused_lists(b["id"]))
        brand_data.append(bd)
    return tpl.TemplateResponse("brands.html", {"request": request, "active": "brands", "brands": brand_data})

@router.post("/brands/add")
async def add_brand(request: Request, name: str = Form("")):
    if name.strip():
        request.app.state.db.add_brand(name.strip())
    return RedirectResponse("/brands", status_code=303)

@router.post("/brands/{bid}/add-domain")
async def add_domain(request: Request, bid: int, domain: str = Form("")):
    if domain.strip():
        request.app.state.db.add_domain(bid, domain.strip())
    return RedirectResponse("/brands", status_code=303)

@router.post("/brands/{bid}/delete")
async def delete_brand(request: Request, bid: int):
    request.app.state.db.delete_brand(bid)
    return RedirectResponse("/brands", status_code=303)

@router.post("/domains/{did}/delete")
async def delete_domain(request: Request, did: int):
    request.app.state.db.delete_domain(did)
    return RedirectResponse("/brands", status_code=303)

@router.post("/domains/{did}/save")
async def save_domain(request: Request, did: int,
                       from_name: str = Form(""), from_email: str = Form(""),
                       reply_to_email: str = Form(""), send_subdomain: str = Form("mail")):
    c = request.app.state.db._conn()
    c.execute("UPDATE domains SET from_name=?, from_email=?, reply_to_email=?, "
              "bounce_subdomain=?, send_subdomain=? WHERE id=?",
              (from_name, from_email, reply_to_email, send_subdomain, send_subdomain, did))
    c.commit()
    return RedirectResponse("/brands", status_code=303)
