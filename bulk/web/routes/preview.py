"""preview page."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("/preview", response_class=HTMLResponse)
async def page(request: Request):
    return request.app.state.templates.TemplateResponse("preview.html",
        {"request": request, "active": "preview", "db": request.app.state.db})
