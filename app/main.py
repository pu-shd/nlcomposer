"""FastAPI entrypoint for the sdc-composer web app.

Serves the single-page composer UI at ``/`` (from ``app/static/index.html``)
and the JSON-driven endpoints used by the front-end:

* ``POST /api/preview/html`` — render a NewsletterIssue to HTML for the
  live preview pane.
* ``POST /api/preview/text`` — plain-text alternative.
* ``POST /api/eml`` — assemble and stream a downloadable ``.eml`` file.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

import httpx

from .eml import build_eml, build_eml_filename, render_html, render_text
from .fetch import ArticleMetadata, fetch_article_metadata, validate_fetch_url
from .models import NewsletterIssue

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"

app = FastAPI(
    title="sdc-composer",
    description=(
        "Compose a SmartDrivingCars newsletter and download a .eml file "
        "matching the historical issue style."
    ),
    version="0.1.0",
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
