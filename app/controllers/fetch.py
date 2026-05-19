"""Auto-fill scrape controllers — article URL and YouTube URL."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from ..fetch import ArticleMetadata, fetch_article_metadata, validate_fetch_url
from ..podcast import (
    YoutubeMetadata,
    fetch_youtube_metadata,
    validate_youtube_url,
    parse_timecodes,
    strip_timecode_lines,
)
from ..youtube import fetch_video_details, extract_video_id


router = APIRouter(prefix="/api/fetch")


# Status codes the SPA can act on more usefully than a generic 502.
# Anything not listed here keeps the original "upstream returned N"
# wording so the editor at least sees the wire-level reason.
def _explain_upstream_status(status: int, url: str) -> str:
    """Render a one-line, user-actionable message for an upstream status.

    The SPA renders ``HTTPException.detail`` verbatim in the article
    card's status line, so the wording goes straight to the editor — no
    extra UI plumbing required.
    """
    if status in (401, 403):
        return (
            "The site refused our request — it likely blocks automated "
            "fetches with a paywall or anti-bot check. Open the URL in a "
            "browser, then copy the headline / byline / pull-quote into "
            "this card by hand. (You can still paste a logo URL or "
            "upload a logo below.)"
        )
    if status == 404:
        return "The article URL returned 404 — double-check the link."
    if status == 429:
        return (
            "The site is rate-limiting us. Wait a minute and try again, "
            "or fill the fields by hand."
        )
    if 500 <= status < 600:
        return (
            f"The site is down or misbehaving ({status}). Try again later "
            "or fill the fields by hand."
        )
    return f"upstream returned {status}"


def _explain_transport_error(exc: httpx.HTTPError, url: str) -> str:
    """One-line message for non-HTTP-status fetch failures (timeout, DNS…)."""
    name = type(exc).__name__
    if isinstance(exc, httpx.ConnectTimeout) or isinstance(exc, httpx.ReadTimeout):
        return (
            "The site took too long to respond. Try again or fill the "
            "fields by hand."
        )
    if isinstance(exc, httpx.ConnectError):
        return (
            "Could not reach the site (DNS or connection refused). "
            "Check the URL and your network."
        )
    if isinstance(exc, httpx.TooManyRedirects):
        return "The site redirects too many times — probably a paywall loop."
    return f"fetch failed ({name}): {exc}"


@router.get("/article")
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
            detail=_explain_upstream_status(e.response.status_code, url),
        )
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=502, detail=_explain_transport_error(e, url)
        )

    return JSONResponse(content=dict(meta))


@router.get("/youtube")
def fetch_youtube(
    url: str = Query(..., description="Public YouTube video URL to scrape."),
) -> JSONResponse:
    """Fetch a YouTube video and return metadata to pre-fill a podcast block.

    Returns a JSON object with optional ``title``, ``summary``,
    ``youtube_video_url``, and a ``timecodes`` list of
    ``{timecode, label, url}`` parsed from MM:SS lines in the description.
    """

    try:
        url = validate_youtube_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Try YouTube API first
    video_id = extract_video_id(url)
    if video_id:
        try:
            details = fetch_video_details(video_id)
            if details:
                description = details["description"]
                meta: YoutubeMetadata = {
                    "title": details["title"],
                    "summary": strip_timecode_lines(description),
                    "youtube_video_url": f"https://www.youtube.com/watch?v={video_id}",
                    "timecodes": parse_timecodes(description, video_id)
                }
                return JSONResponse(content=dict(meta))
        except Exception as e:
            # Fallback to scraping if API fails
            print(f"YouTube API failed, falling back to scraping: {e}")

    try:
        meta: YoutubeMetadata = fetch_youtube_metadata(url)
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=_explain_upstream_status(e.response.status_code, url),
        )
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=502, detail=_explain_transport_error(e, url)
        )

    return JSONResponse(content=dict(meta))
