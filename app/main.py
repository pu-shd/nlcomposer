"""FastAPI composition root for the sdc-composer web app.

This module is intentionally thin — it wires the pieces together but
contains no route logic of its own. The MVC split lives here:

* **Models** — ``app/models.py``, ``app/settings.py``, ``app/*_store.py``.
* **Views**  — ``app/templates/`` (Jinja), ``app/static/`` (SPA), and
  ``app/eml.py`` (HTML / text / .eml renderers).
* **Controllers** — one ``APIRouter`` per resource under
  ``app/controllers/``; this module mounts each of them.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .controllers import (
    archive as archive_ctrl,
    drafts as drafts_ctrl,
    eml as eml_ctrl,
    fetch as fetch_ctrl,
    media as media_ctrl,
    pages as pages_ctrl,
    preview as preview_ctrl,
    settings as settings_ctrl,
    templates as templates_ctrl,
)
from .db import connect, init_db
from .template_store import seed_default_templates


_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    try:
        init_db(conn)
        seed_default_templates(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="sdc-composer",
    description=(
        "Compose a SmartDrivingCars newsletter and download a .eml file "
        "matching the historical issue style."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


for _ctrl in (
    pages_ctrl,
    preview_ctrl,
    eml_ctrl,
    settings_ctrl,
    templates_ctrl,
    drafts_ctrl,
    archive_ctrl,
    fetch_ctrl,
    media_ctrl,
):
    app.include_router(_ctrl.router)
