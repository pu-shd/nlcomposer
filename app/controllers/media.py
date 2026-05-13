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

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
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
    sniff_image_kind,
    update_alt_text,
    upsert_by_url,
)
from ..models import SanitizedHttpUrl
from ..rate_limit import configure_for, upload_limiter
from ..settings import AppSettings, load_settings
from .utils import or_404


router = APIRouter(prefix="/api/media")


def _client_ip(request: Request) -> str:
    """Stable per-client key for the rate limiter.

    Uses the socket peer address by default; falls back to the
    ``X-Forwarded-For`` first hop when the app is behind a known proxy
    (set ``SDC_TRUSTED_FORWARDED=1`` to opt in — off by default to
    prevent spoofing on a direct-exposed dev server).
    """
    import os

    if os.environ.get("SDC_TRUSTED_FORWARDED") == "1":
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    client = request.client
    return client.host if client else "unknown"


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

    Returns 403 when ``AppSettings.media_url_imports_enabled`` is off —
    operator opt-out for instances that should not reach back out to
    arbitrary remote hosts.
    """
    settings = load_settings(conn)
    if not settings.media_url_imports_enabled:
        raise HTTPException(
            status_code=403,
            detail=(
                "URL imports are disabled in Settings; upload the file "
                "directly via /api/media/upload instead."
            ),
        )
    return upsert_by_url(conn, url=str(body.url), kind=body.kind)


@router.post("/upload", response_model=MediaItem, status_code=201)
async def api_upload_media(
    request: Request,
    kind: MediaKind = Form(...),
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_db),
) -> MediaItem:
    """Persist an uploaded image. ``url`` stays NULL on the resulting row.

    Gating (all driven by :class:`AppSettings`):

    * **Rate limit** — per-client IP, ``media_upload_rate_per_minute``
      requests per rolling 60s. 429 with ``Retry-After`` when exceeded.
    * **Allowed types** — ``media_upload_allowed_types`` (list of MIME
      strings). The client-declared header AND a magic-byte sniff of
      the payload must both match an entry; mismatch is 415.
    * **Size cap** — ``media_upload_max_bytes`` is the upper bound on a
      single payload; 413 when exceeded.

    Empty bodies are 400; everything else round-trips the persisted
    :class:`MediaItem` so the SPA can update the row in place.
    """
    settings = load_settings(conn)

    # 1. Rate limit BEFORE reading the body so a flood doesn't burn
    #    bandwidth waiting on the size check.
    configure_for(capacity=settings.media_upload_rate_per_minute)
    decision = upload_limiter.check(_client_ip(request))
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Upload rate limit exceeded "
                f"({decision.limit} per 60s). Try again in "
                f"{decision.retry_after:.0f}s."
            ),
            headers={"Retry-After": str(int(decision.retry_after) + 1)},
        )

    # 2. Header-level mimetype gate — cheap rejection before reading
    #    the file body. We re-validate against the magic bytes below.
    allowed_types = frozenset(t.lower() for t in settings.media_upload_allowed_types)
    declared_type = (file.content_type or "").lower()
    if declared_type not in allowed_types:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported content_type {declared_type!r}; "
                f"allowed: {sorted(allowed_types)}."
            ),
        )

    content_bytes = await file.read()
    if not content_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # 3. Size cap — done after read() because UploadFile streams; an
    #    early Content-Length check would still let a chunked client
    #    sneak past, so we cap on actual bytes received.
    if len(content_bytes) > settings.media_upload_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Uploaded file exceeds {settings.media_upload_max_bytes} "
                f"bytes ({len(content_bytes)} received)."
            ),
        )

    # 4. Magic-byte verification — defends against a deliberately
    #    mislabeled payload (e.g. an HTML page sent as image/png).
    sniffed = sniff_image_kind(content_bytes)
    if sniffed is None or sniffed not in allowed_types:
        raise HTTPException(
            status_code=415,
            detail=(
                "Uploaded bytes do not match a supported image format "
                f"(declared {declared_type!r}, sniffed "
                f"{sniffed or 'unknown'!r})."
            ),
        )
    if sniffed != declared_type:
        # Trust the bytes over the header — but flag the discrepancy
        # so we don't quietly persist content under a misleading type.
        raise HTTPException(
            status_code=415,
            detail=(
                f"Content-Type/body mismatch: declared {declared_type!r} "
                f"but bytes look like {sniffed!r}."
            ),
        )

    return create_upload(
        conn,
        kind=kind,
        filename=file.filename or "upload",
        content_type=sniffed,
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
    or_404(get_media(conn, media_id), "Media")
    payload = get_media_bytes(conn, media_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Media bytes not cached")
    body, ctype = payload
    return Response(
        content=body,
        media_type=ctype or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=300"},
    )
