"""Content Pools — names, subjects, spintax, alt texts."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

POOL_TYPES = ["names", "subjects", "spintax", "alt_texts"]


@router.get("/content", response_class=HTMLResponse)
async def content_page(request: Request):
    db = request.app.state.db
    pools = {}
    for pt in POOL_TYPES:
        pools[pt] = [dict(p) for p in db.get_pools(pt)]
        if not pools[pt]:
            pools[pt] = [{"name": "default", "content": db.get_pool(pt)}]
    return request.app.state.templates.TemplateResponse(request, "content.html", {
        "active": "content", "pools": pools, "pool_types": POOL_TYPES, "db": db,
    })


@router.post("/content/save")
async def save_pool(request: Request, pool_type: str = Form(""),
                    name: str = Form("default"), content: str = Form("")):
    if pool_type in POOL_TYPES:
        request.app.state.db.set_pool(pool_type, name.strip() or "default", content)
    return RedirectResponse("/content", status_code=303)


@router.post("/content/{pid}/delete")
async def delete_pool(request: Request, pid: int):
    request.app.state.db.delete_pool(pid)
    return RedirectResponse("/content", status_code=303)
