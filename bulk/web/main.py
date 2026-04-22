"""Bulk Mailer Web — FastAPI + HTMX + Jinja2."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

from bulk.mailer.db_manager import BulkDBManager

from .routes import mailings, brands, smtp, lists, composer, macros, preview, cloudflare, logs, macro_help

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
db = BulkDBManager(os.path.join(_project_root, "bulk.db"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = db
    yield
    db.close()

app = FastAPI(title="Bulk Mailer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
app.state.templates = templates

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

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "mailings.html", {
        "active": "mailings", "mailings": [], "db": db,
        "brands": db.get_brands(), "domains": db.get_domains(),
        "lists": db.get_lists(), "smtps": db.get_smtps(),
        "templates": db.get_templates(),
    })
