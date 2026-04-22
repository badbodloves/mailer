"""composer page."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("/composer", response_class=HTMLResponse)
async def page(request: Request):
    return request.app.state.templates.TemplateResponse("composer.html",
        {"request": request, "active": "composer", "db": request.app.state.db})
