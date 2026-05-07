"""HTML page + health controller."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse


_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"

router = APIRouter()


@router.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@router.get("/", response_class=HTMLResponse)
def index() -> str:
    if not _INDEX_HTML.exists():
        raise HTTPException(status_code=500, detail="composer index.html missing")
    return _INDEX_HTML.read_text(encoding="utf-8")
