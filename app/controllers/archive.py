"""Archived-issue list/get/download/delete controller."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from ..archive_store import (
    ArchiveEntry,
    ArchiveSummary,
    delete_archive,
    get_archive,
    get_archive_eml,
    list_archive,
)
from ..db import get_db


router = APIRouter(prefix="/api/archive")


class HighlightCandidate(BaseModel):
    """One article from an archived issue, ready to attach as a highlight.

    Maps 1:1 to a Logos & Media-style autocomplete entry: each candidate
    carries everything the SPA needs to populate a HighlightRef without
    a follow-up fetch.
    """

    archive_id: int
    article_index: int
    headline: str
    url: str
    issue_date: Optional[date]
    issue_year: Optional[int]
    issue_number: Optional[int]
    issue_title: Optional[str]


@router.get("", response_model=list[ArchiveSummary])
def api_list_archive(
    conn: sqlite3.Connection = Depends(get_db),
) -> list[ArchiveSummary]:
    """Return archived issues, most-recently-saved first, without payloads."""
    return list_archive(conn)


# The /highlight-candidates literal must be declared BEFORE the
# /{archive_id} parametric route; otherwise Starlette tries to match
# the literal as an int and 422s before falling through. This isn't a
# routing quirk we can fix with @router.get("/highlight-candidates",
# tags=...); it has to come first in the source.
@router.get("/highlight-candidates", response_model=list[HighlightCandidate])
def api_highlight_candidates(
    q: str = Query("", description="Substring to match against article headlines (case-insensitive)."),
    days: int = Query(
        365,
        ge=1,
        le=3650,
        description="Look back this many days into the archive (default 365 ≈ previous year).",
    ),
    limit: int = Query(
        50,
        ge=1,
        le=200,
        description="Maximum number of candidates to return after dedup.",
    ),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[HighlightCandidate]:
    """Search the recent archive for articles to feature as highlights.

    Walks every archived issue within ``days``, surfaces each article in
    payload order, optionally filters by case-insensitive ``q`` substring
    against the headline, then dedups by URL keeping the newest
    occurrence so the same canonical article never shows up twice in the
    autocomplete dropdown.

    Skips archive entries whose payload isn't decodable or doesn't carry
    an ``articles`` list — the endpoint silently drops them rather than
    failing the whole search.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = conn.execute(
        """
        SELECT id, payload, created_at
        FROM archive
        WHERE created_at >= ?
        ORDER BY created_at DESC, id DESC
        """,
        (cutoff.isoformat(),),
    ).fetchall()

    needle = q.strip().lower()
    seen_urls: dict[str, HighlightCandidate] = {}
    ordered: list[HighlightCandidate] = []
    import json as _json

    for row in rows:
        try:
            payload = _json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        articles = payload.get("articles") or []
        if not isinstance(articles, list):
            continue
        raw_date = payload.get("issue_date")
        issue_date_val: Optional[date] = None
        if isinstance(raw_date, str):
            try:
                issue_date_val = date.fromisoformat(raw_date[:10])
            except ValueError:
                issue_date_val = None
        elif isinstance(raw_date, date):
            issue_date_val = raw_date

        for idx, art in enumerate(articles):
            if not isinstance(art, dict):
                continue
            headline = str(art.get("headline") or "").strip()
            url = str(art.get("url") or "").strip()
            if not headline or not url:
                continue
            if needle and needle not in headline.lower():
                continue
            if url in seen_urls:
                continue
            cand = HighlightCandidate(
                archive_id=int(row["id"]),
                article_index=idx,
                headline=headline,
                url=url,
                issue_date=issue_date_val,
                issue_year=(
                    int(payload["issue_year"])
                    if isinstance(payload.get("issue_year"), int)
                    else None
                ),
                issue_number=(
                    int(payload["issue_number"])
                    if isinstance(payload.get("issue_number"), int)
                    else None
                ),
                issue_title=payload.get("issue_title") or None,
            )
            seen_urls[url] = cand
            ordered.append(cand)
            if len(ordered) >= limit:
                return ordered
    return ordered


@router.get("/{archive_id}", response_model=ArchiveEntry)
def api_get_archive(
    archive_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> ArchiveEntry:
    entry = get_archive(conn, archive_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Archive entry not found")
    return entry


@router.get("/{archive_id}/download")
def api_download_archive(
    archive_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """Stream the stored ``.eml`` bytes back to the user as a download."""
    found = get_archive_eml(conn, archive_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Archive entry not found")
    filename, eml_bytes = found
    return Response(
        content=eml_bytes,
        media_type="message/rfc822",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/{archive_id}", status_code=204)
def api_delete_archive(
    archive_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    deleted = delete_archive(conn, archive_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Archive entry not found")
    return Response(status_code=204)
