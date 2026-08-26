"""Bulk Mailer Web — FastAPI + HTMX + Jinja2."""
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

from bulk.mailer.db_manager import BulkDBManager

from .routes import mailings, brands, smtp, lists, composer, macros, preview, cloudflare, logs, macro_help
from .routes import auth, profile, dynadot, dns, warmup, fast_deploy, expurgate, cloudinary, pdf_variator, spaceship

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
db = BulkDBManager(os.path.join(_project_root, "bulk.db"))

PUBLIC_PATHS = {"/login", "/setup", "/static"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        request.state.user = None

        if path.startswith("/static"):
            return await call_next(request)

        if path in ("/login", "/setup"):
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
    yield
    db.close()

app = FastAPI(title="Bulk Mailer", lifespan=lifespan)
app.state.db = db

app.add_middleware(AuthMiddleware)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(os.path.join(_static_dir, "uploads"), exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
app.state.templates = templates

app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(mailings.router)
app.include_router(brands.router)
app.include_router(smtp.router)
app.include_router(lists.router)
app.include_router(composer.router)
app.include_router(macros.router)
app.include_router(preview.router)
app.include_router(cloudflare.router)
app.include_router(logs.router)
app.include_router(macro_help.router)
app.include_router(dynadot.router)
app.include_router(dns.router)
app.include_router(warmup.router)
app.include_router(fast_deploy.router)
app.include_router(expurgate.router)
app.include_router(cloudinary.router)
app.include_router(pdf_variator.router)
app.include_router(spaceship.router)


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "mailings.html", {
        "active": "mailings", "mailings": [], "db": db,
        "brands": db.get_brands(), "domains": db.get_domains(),
        "lists": db.get_lists(), "smtps": db.get_smtps(),
        "templates": db.get_templates(),
    })
