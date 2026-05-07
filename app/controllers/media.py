"""Media Library REST controller.

Endpoints
---------
``GET    /api/media``                 — list all media items (no bytes).
``GET    /api/media/{id}``            — fetch one media item (no bytes).
``POST   /api/media``                 — register a URL-sourced item (idempotent
                                        upsert). Body: ``{kind, url}``.
``POST   /api/media/upload``          — upload PNG/JPG bytes directly.
                                        Multipart fields: ``kind``, ``file``.
``PUT    /api/media/{id}``            — update the alt text ("Image
                                        description"). Body: ``{alt_text}``.
``DELETE /api/media/{id}``            — remove a media item.
``GET    /api/media/{id}/content``    — stream the cached PNG/JPG bytes.
                                        Returns 404 while pending/failed.

All write endpoints round-trip the resulting :class:`MediaItem` so the SPA
can update its row without re-listing.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from ..db import get_db
from ..media_store import (
    MediaItem,
    MediaKind,
    create_upload,
    delete_media,
    get_media,
    get_media_bytes,
    list_media,
    update_alt_text,
    upsert_by_url,
)
from ..models import SanitizedHttpUrl


router = APIRouter(prefix="/api/media")


_ALLOWED_UPLOAD_TYPES = frozenset({"image/png", "image/jpeg"})
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MiB — sponsor logos / banners only


class MediaUrlCreate(BaseModel):
    """Body for ``POST /api/media`` — register a URL-sourced item."""

    kind: MediaKind
    url: SanitizedHttpUrl


class MediaAltUpdate(BaseModel):
    """Body for ``PUT /api/media/{id}`` — set the Image description."""

    alt_text: str = Field(default="", max_length=500)


@router.get("", response_model=list[MediaItem])
def api_list_media(
    conn: sqlite3.Connection = Depends(get_db),
) -> list[MediaItem]:
    return list_media(conn)


@router.get("/{media_id}", response_model=MediaItem)
def api_get_media(
    media_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> MediaItem:
    item = get_media(conn, media_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Media not found")
    return item


@router.post("", response_model=MediaItem, status_code=201)
def api_create_media_by_url(
    body: MediaUrlCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> MediaItem:
    """Register or return the existing URL-sourced library entry.

    Idempotent — re-posting the same URL returns the existing row with its
    original ``kind`` preserved, so a logo URL referenced later from a
    static block does not flip to ``image``.
    """
    return upsert_by_url(conn, url=str(body.url), kind=body.kind)


@router.post("/upload", response_model=MediaItem, status_code=201)
async def api_upload_media(
    kind: MediaKind = Form(...),
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_db),
) -> MediaItem:
    """Persist an uploaded PNG/JPG. ``url`` stays NULL on the resulting row."""
    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_UPLOAD_TYPES:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported content_type {content_type!r}; "
                "only image/png and image/jpeg are allowed."
            ),
        )
    content_bytes = await file.read()
    if not content_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Uploaded file exceeds {_MAX_UPLOAD_BYTES} bytes.",
        )
    return create_upload(
        conn,
        kind=kind,
        filename=file.filename or "upload",
        content_type=content_type,
        content_bytes=content_bytes,
    )


@router.put("/{media_id}", response_model=MediaItem)
def api_update_media_alt(
    media_id: int,
    body: MediaAltUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> MediaItem:
    updated = update_alt_text(conn, media_id, body.alt_text)
    if updated is None:
        raise HTTPException(status_code=404, detail="Media not found")
    return updated


@router.delete("/{media_id}", status_code=204)
def api_delete_media(
    media_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    if not delete_media(conn, media_id):
        raise HTTPException(status_code=404, detail="Media not found")
    return Response(status_code=204)


@router.get("/{media_id}/content")
def api_get_media_content(
    media_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """Stream the cached PNG/JPG bytes.

    Returns 404 when the row does not exist or while bytes are still
    pending — callers fall back to the source URL in that case.
    """
    if get_media(conn, media_id) is None:
        raise HTTPException(status_code=404, detail="Media not found")
    payload = get_media_bytes(conn, media_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Media bytes not cached")
    body, ctype = payload
    return Response(
        content=body,
        media_type=ctype or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=300"},
    )
