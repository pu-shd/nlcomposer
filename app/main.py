"""FastAPI entrypoint for the sdc-composer web app.

Serves the single-page composer UI at ``/`` (from ``app/static/index.html``)
and the JSON-driven endpoints used by the front-end:

* ``POST /api/preview/html`` — render a NewsletterIssue to HTML for the
  live preview pane.
* ``POST /api/preview/text`` — plain-text alternative.
* ``POST /api/eml`` — assemble and stream a downloadable ``.eml`` file.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

import httpx

from .db import connect, get_db, init_db
from .eml import build_eml, build_eml_filename, render_html, render_text
from .fetch import ArticleMetadata, fetch_article_metadata, validate_fetch_url
from .models import NewsletterIssue
from .podcast import (
    YoutubeMetadata,
    fetch_youtube_metadata,
    validate_youtube_url,
)
from .settings import AppSettings, load_settings, save_settings
from .template_store import (
    BuiltinDeleteError,
    Template,
    TemplateCreate,
    TemplateNameConflictError,
    TemplateSummary,
    TemplateUpdate,
    create_template,
    delete_template,
    get_template,
    list_templates,
    seed_default_templates,
    update_template,
)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"


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


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    if not _INDEX_HTML.exists():
        raise HTTPException(status_code=500, detail="composer index.html missing")
    return _INDEX_HTML.read_text(encoding="utf-8")


@app.post("/api/preview/html", response_class=HTMLResponse)
def preview_html(issue: NewsletterIssue) -> str:
    return render_html(issue)


@app.post("/api/preview/text", response_class=PlainTextResponse)
def preview_text(issue: NewsletterIssue) -> str:
    return render_text(issue)


@app.post("/api/eml")
def download_eml(issue: NewsletterIssue) -> Response:
    eml_bytes = build_eml(issue)
    filename = build_eml_filename(issue)
    return Response(
        content=eml_bytes,
        media_type="message/rfc822",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/settings", response_model=AppSettings)
def get_settings(conn: sqlite3.Connection = Depends(get_db)) -> AppSettings:
    """Return current app-wide settings, or built-in defaults if unset."""
    return load_settings(conn)


@app.put("/api/settings", response_model=AppSettings)
def put_settings(
    settings: AppSettings,
    conn: sqlite3.Connection = Depends(get_db),
) -> AppSettings:
    """Replace the stored settings with the supplied payload."""
    return save_settings(conn, settings)


@app.get("/api/templates", response_model=list[TemplateSummary])
def api_list_templates(
    conn: sqlite3.Connection = Depends(get_db),
) -> list[TemplateSummary]:
    """Return all templates (builtins first), without their payloads."""
    return list_templates(conn)


@app.get("/api/templates/{template_id}", response_model=Template)
def api_get_template(
    template_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Template:
    t = get_template(conn, template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return t


@app.post("/api/templates", response_model=Template, status_code=201)
def api_create_template(
    body: TemplateCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> Template:
    try:
        return create_template(conn, body)
    except TemplateNameConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.put("/api/templates/{template_id}", response_model=Template)
def api_update_template(
    template_id: int,
    body: TemplateUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> Template:
    try:
        updated = update_template(conn, template_id, body)
    except TemplateNameConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return updated


@app.delete("/api/templates/{template_id}", status_code=204)
def api_delete_template(
    template_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    try:
        deleted = delete_template(conn, template_id)
    except BuiltinDeleteError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Template not found")
    return Response(status_code=204)


@app.get("/api/fetch/article")
def fetch_article(
    url: str = Query(..., description="Public article URL to scrape."),
) -> JSONResponse:
    """Fetch an article and return metadata to pre-fill an Article entry.

    Returns a JSON object with optional ``headline``, ``byline``,
    ``pull_quote``, ``publication_logo_url``, and ``read_more_url`` keys.
    The composer UI uses this to populate empty fields when the user
    clicks "Fetch" next to an article URL.
    """

    try:
        validate_fetch_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        meta: ArticleMetadata = fetch_article_metadata(url)
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"upstream returned {e.response.status_code}",
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"fetch failed: {e}")

    return JSONResponse(content=dict(meta))


@app.get("/api/fetch/youtube")
def fetch_youtube(
    url: str = Query(..., description="Public YouTube video URL to scrape."),
) -> JSONResponse:
    """Fetch a YouTube video and return metadata to pre-fill a podcast block.

    Returns a JSON object with optional ``title``, ``summary``,
    ``youtube_video_url``, and a ``timecodes`` list of
    ``{timecode, label, url}`` parsed from MM:SS lines in the description.
    """

    try:
        validate_youtube_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        meta: YoutubeMetadata = fetch_youtube_metadata(url)
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"upstream returned {e.response.status_code}",
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"fetch failed: {e}")

    return JSONResponse(content=dict(meta))
