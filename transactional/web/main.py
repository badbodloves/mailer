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
from .routes import campaigns, smtps, leads, templates, auth, logos, redirects, macros, proxies, pools, config as config_route, settings, ai, admin, testlab, bounces, htmlgen, inboxtest, exporter, cloudinary, smtp_check

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
        if not user.get("is_active", 1):
            return RedirectResponse("/login", status_code=303)

        if not auth.check_permission(user, path):
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("Access denied", status_code=403)

        request.state.user = user
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    db.close()

app = FastAPI(title="Transactional Mailer", lifespan=lifespan)
app.state.db = db

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    tb = traceback.format_exc()
    logger = logging.getLogger("trans.error")
    logger.error("Unhandled: %s\n%s", exc, tb)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(f"Error: {exc}\n\n{tb}", status_code=500)

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
app.include_router(macros.router)
app.include_router(logos.router)
app.include_router(redirects.router)
app.include_router(proxies.router)
app.include_router(pools.router)
app.include_router(ai.router)
app.include_router(config_route.router)
app.include_router(settings.router)
app.include_router(testlab.router)
app.include_router(bounces.router)
app.include_router(admin.router)
app.include_router(htmlgen.router)
app.include_router(inboxtest.router)
app.include_router(exporter.router)
app.include_router(cloudinary.router)
app.include_router(smtp_check.router)


@app.get("/")
async def index(request: Request):
    return RedirectResponse("/campaigns", status_code=303)
