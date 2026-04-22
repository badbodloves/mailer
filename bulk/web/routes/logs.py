"""logs page."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("/logs", response_class=HTMLResponse)
async def page(request: Request):
    return request.app.state.templates.TemplateResponse("logs.html",
        {"request": request, "active": "logs", "db": request.app.state.db})
