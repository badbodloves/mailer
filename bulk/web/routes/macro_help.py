"""macro_help page."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("/help", response_class=HTMLResponse)
async def page(request: Request):
    return request.app.state.templates.TemplateResponse("macro_help.html",
        {"request": request, "active": "macro_help", "db": request.app.state.db})
