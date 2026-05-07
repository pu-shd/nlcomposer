"""Live-preview controller — HTML and plain-text rendering."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..eml import render_html, render_text
from ..models import NewsletterIssue


router = APIRouter(prefix="/api/preview")


@router.post("/html", response_class=HTMLResponse)
def preview_html(issue: NewsletterIssue) -> str:
    return render_html(issue)


@router.post("/text", response_class=PlainTextResponse)
def preview_text(issue: NewsletterIssue) -> str:
    return render_text(issue)
