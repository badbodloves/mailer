"""cloudflare page."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("/cloudflare", response_class=HTMLResponse)
async def page(request: Request):
    return request.app.state.templates.TemplateResponse("cloudflare.html",
        {"request": request, "active": "cloudflare", "db": request.app.state.db})
