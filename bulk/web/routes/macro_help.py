"""Macro Help — Static reference page for all available macros."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    return request.app.state.templates.TemplateResponse("macro_help.html", {
        "request": request, "active": "help", "db": request.app.state.db,
    })
