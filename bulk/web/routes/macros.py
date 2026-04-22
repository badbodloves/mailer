"""macros page."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("/macros", response_class=HTMLResponse)
async def page(request: Request):
    return request.app.state.templates.TemplateResponse("macros.html",
        {"request": request, "active": "macros", "db": request.app.state.db})
