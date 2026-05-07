"""Media Library — cached PNG/JPG assets used by Article logos and
Static-block images.

A media item is one of two flavors:

* **URL-sourced** — the user pasted a logo or image URL. The store records
  the URL and a ``pending`` status; a background fetcher later transitions
  the row to ``ready`` (with cached bytes + content type) or ``failed``
  (with a short error message). Re-providing the same URL returns the
  existing item — :func:`upsert_by_url` is idempotent on URL match.

* **Uploaded** — the user uploaded a file directly. ``url`` is ``NULL``,
  bytes are written immediately, and the status starts as ``ready``.

Alt text (the "Image description" required for use) is recorded on every
item and starts empty; the UI gates use of an item on a non-empty
``alt_text``.

The module is named ``media_store`` (not ``media``) for symmetry with
:mod:`app.draft_store`, :mod:`app.template_store`, and :mod:`app.archive_store`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .models import SanitizedHttpUrl


MediaKind = Literal["logo", "image"]
MediaStatus = Literal["pending", "ready", "failed"]


_ALLOWED_CONTENT_TYPES = frozenset({"image/png", "image/jpeg"})


class MediaItem(BaseModel):
    """List/detail view of a media item — bytes are *not* serialized.

    The cached bytes are streamed via a separate ``/api/media/{id}/content``
    endpoint so listing the library stays cheap and JSON payloads remain
    base64-free.
    """

    id: int
    kind: MediaKind
    url: Optional[SanitizedHttpUrl] = None
    filename: Optional[str] = None
    content_type: Optional[str] = None
    has_bytes: bool = Field(
        default=False,
        description=(
            "True when the row carries cached bytes. Lets the UI decide "
            "whether to render the placeholder thumbnail."
        ),
    )
    alt_text: str = ""
    status: MediaStatus = "pending"
    error: Optional[str] = None
    created_at: str
    updated_at: str

    def is_usable(self) -> bool:
        """An item is usable in a newsletter once its alt text is filled in.

        Status is intentionally *not* gated here — pending/failed URL items
        can still be referenced; the renderer falls back to the URL with a
        warning when bytes are unavailable.
        """
        return bool(self.alt_text.strip())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_item(row: sqlite3.Row) -> MediaItem:
    return MediaItem(
        id=row["id"],
        kind=row["kind"],
        url=row["url"],
        filename=row["filename"],
        content_type=row["content_type"],
        has_bytes=row["content_bytes"] is not None
        and len(bytes(row["content_bytes"])) > 0,
        alt_text=row["alt_text"] or "",
        status=row["status"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_media(conn: sqlite3.Connection) -> list[MediaItem]:
    """Return every media item, most-recently-touched first."""
    rows = conn.execute(
        """
        SELECT id, kind, url, filename, content_type, content_bytes,
               alt_text, status, error, created_at, updated_at
        FROM media
        ORDER BY updated_at DESC, id DESC
        """
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def get_media(conn: sqlite3.Connection, media_id: int) -> Optional[MediaItem]:
    row = conn.execute(
        """
        SELECT id, kind, url, filename, content_type, content_bytes,
               alt_text, status, error, created_at, updated_at
        FROM media WHERE id = ?
        """,
        (media_id,),
    ).fetchone()
    return _row_to_item(row) if row else None


def get_media_by_url(conn: sqlite3.Connection, url: str) -> Optional[MediaItem]:
    row = conn.execute(
        """
        SELECT id, kind, url, filename, content_type, content_bytes,
               alt_text, status, error, created_at, updated_at
        FROM media WHERE url = ?
        """,
        (url,),
    ).fetchone()
    return _row_to_item(row) if row else None


def get_media_bytes(
    conn: sqlite3.Connection, media_id: int
) -> Optional[tuple[bytes, Optional[str]]]:
    """Return ``(content_bytes, content_type)`` for a media row, or ``None``.

    Returns ``None`` either when the row does not exist *or* when bytes are
    not yet cached (pending/failed URL items). Callers should fall back to
    the source URL in that case.
    """
    row = conn.execute(
        "SELECT content_bytes, content_type FROM media WHERE id = ?",
        (media_id,),
    ).fetchone()
    if row is None or row["content_bytes"] is None:
        return None
    return bytes(row["content_bytes"]), row["content_type"]


def upsert_by_url(
    conn: sqlite3.Connection,
    *,
    url: str,
    kind: MediaKind,
) -> MediaItem:
    """Insert a pending URL-sourced item, or return the existing one.

    Idempotent — the same URL always maps to the same row. The ``kind`` of
    an existing row is left untouched (the first reference wins) so that
    e.g. a URL first registered as a static-block image does not flip to
    ``logo`` because an article happened to point at it later.
    """
    existing = get_media_by_url(conn, url)
    if existing is not None:
        return existing

    now = _now_iso()
    cur = conn.execute(
        """
        INSERT INTO media
            (kind, url, status, alt_text, created_at, updated_at)
        VALUES (?, ?, 'pending', '', ?, ?)
        """,
        (kind, url, now, now),
    )
    conn.commit()
    return get_media(conn, cur.lastrowid)  # type: ignore[arg-type,return-value]


def create_upload(
    conn: sqlite3.Connection,
    *,
    kind: MediaKind,
    filename: str,
    content_type: str,
    content_bytes: bytes,
) -> MediaItem:
    """Persist a user-uploaded image. PNG/JPG only — caller validates upstream."""
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise ValueError(
            f"Unsupported content_type {content_type!r}; allowed: "
            f"{sorted(_ALLOWED_CONTENT_TYPES)}"
        )
    if not content_bytes:
        raise ValueError("content_bytes must be non-empty for uploads")

    now = _now_iso()
    cur = conn.execute(
        """
        INSERT INTO media
            (kind, filename, content_type, content_bytes, status, alt_text,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, 'ready', '', ?, ?)
        """,
        (kind, filename, content_type, content_bytes, now, now),
    )
    conn.commit()
    return get_media(conn, cur.lastrowid)  # type: ignore[arg-type,return-value]


def update_alt_text(
    conn: sqlite3.Connection, media_id: int, alt_text: str
) -> Optional[MediaItem]:
    """Set the user-supplied Image description. Returns the updated row."""
    cur = conn.execute(
        "UPDATE media SET alt_text = ?, updated_at = ? WHERE id = ?",
        (alt_text, _now_iso(), media_id),
    )
    if cur.rowcount == 0:
        return None
    conn.commit()
    return get_media(conn, media_id)


def mark_ready(
    conn: sqlite3.Connection,
    media_id: int,
    *,
    content_type: str,
    content_bytes: bytes,
) -> Optional[MediaItem]:
    """Transition a pending row to ``ready`` with cached bytes."""
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise ValueError(
            f"Unsupported content_type {content_type!r}; allowed: "
            f"{sorted(_ALLOWED_CONTENT_TYPES)}"
        )
    cur = conn.execute(
        """
        UPDATE media
           SET status = 'ready',
               content_type = ?,
               content_bytes = ?,
               error = NULL,
               updated_at = ?
         WHERE id = ?
        """,
        (content_type, content_bytes, _now_iso(), media_id),
    )
    if cur.rowcount == 0:
        return None
    conn.commit()
    return get_media(conn, media_id)


def mark_failed(
    conn: sqlite3.Connection, media_id: int, *, error: str
) -> Optional[MediaItem]:
    """Transition a pending row to ``failed`` with a short error string."""
    cur = conn.execute(
        """
        UPDATE media
           SET status = 'failed',
               error = ?,
               updated_at = ?
         WHERE id = ?
        """,
        (error[:500], _now_iso(), media_id),
    )
    if cur.rowcount == 0:
        return None
    conn.commit()
    return get_media(conn, media_id)


def delete_media(conn: sqlite3.Connection, media_id: int) -> bool:
    cur = conn.execute("DELETE FROM media WHERE id = ?", (media_id,))
    conn.commit()
    return cur.rowcount > 0
