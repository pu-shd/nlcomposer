"""HTML page + health controller."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import HTMLResponse, PlainTextResponse


_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"

router = APIRouter()


@router.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@router.get("/", response_class=HTMLResponse)
def index(response: Response) -> str:
    if not _INDEX_HTML.exists():
        raise HTTPException(status_code=500, detail="composer index.html missing")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return _INDEX_HTML.read_text(encoding="utf-8")
