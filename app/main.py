"""FastAPI entrypoint for the sdc-composer web app.

This iteration delivers the skeleton: a health endpoint, a JSON-driven
``/api/preview/html`` endpoint that returns the rendered HTML, and a
``/api/eml`` endpoint that returns the assembled ``.eml`` for download.
A real form-driven UI is added in a follow-up task (``ui:composer``).
"""

from __future__ import annotations

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .eml import build_eml, build_eml_filename, render_html, render_text
from .models import NewsletterIssue

_STATIC_DIR = Path(__file__).resolve().parent / "static"

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
    """Placeholder landing page until the composer UI lands."""
    return (
        "<!doctype html><html><head><title>sdc-composer</title></head><body>"
        "<h1>sdc-composer</h1>"
        "<p>POST a NewsletterIssue JSON to <code>/api/preview/html</code> for a"
        " rendered preview, or to <code>/api/eml</code> to download the"
        " assembled <code>.eml</code> file.</p>"
        '<p>API docs: <a href="/docs">/docs</a></p>'
        "</body></html>"
    )


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
