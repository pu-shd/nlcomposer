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
)


router = APIRouter(prefix="/api/fetch")


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
            detail=f"upstream returned {e.response.status_code}",
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"fetch failed: {e}")

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
