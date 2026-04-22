"""Transactional Mailer Web — FastAPI + HTMX + Jinja2."""
import sys
import os
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
from starlette.middleware.base import BaseHTTPMiddleware

from .db import TransDB
from .routes import campaigns, smtps, leads, templates, content, settings, auth

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
db = TransDB(os.path.join(_project_root, "trans.db"))


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        request.state.user = None

        if path.startswith("/static") or path in ("/login", "/setup"):
            return await call_next(request)

        if db.user_count() == 0:
            return RedirectResponse("/login", status_code=303)

        user = auth.get_current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)

        request.state.user = user
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = db
    yield
    db.close()

app = FastAPI(title="Transactional Mailer")

app.add_middleware(AuthMiddleware)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# Reuse bulk mailer CSS
_bulk_css = os.path.join(_project_root, "bulk", "web", "static", "style.css")
if os.path.isfile(_bulk_css):
    import shutil
    shutil.copy2(_bulk_css, os.path.join(_static_dir, "style.css"))

tpl_dir = os.path.join(os.path.dirname(__file__), "templates")
jinja_templates = Jinja2Templates(directory=tpl_dir)
app.state.templates = jinja_templates

app.include_router(auth.router)
app.include_router(campaigns.router)
app.include_router(smtps.router)
app.include_router(leads.router)
app.include_router(templates.router)
app.include_router(content.router)
app.include_router(settings.router)


@app.get("/")
async def index(request: Request):
    return RedirectResponse("/campaigns", status_code=303)
