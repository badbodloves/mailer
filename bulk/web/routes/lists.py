"""lists page."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("/lists", response_class=HTMLResponse)
async def page(request: Request):
    return request.app.state.templates.TemplateResponse("lists.html",
        {"request": request, "active": "lists", "db": request.app.state.db})
