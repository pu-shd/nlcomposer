"""Finalized issues — the archive of every ``.eml`` the user has downloaded.

Stored in the ``archive`` SQLite table. Each row holds the rendered ``.eml``
bytes (so the user can re-download the exact message that was saved) plus
the JSON payload of the :class:`~app.models.NewsletterIssue` it came from
(so a future build can prefill the form to compose a follow-up).

Auto-archive happens at ``POST /api/eml`` time when
``settings.auto_archive_on_download`` is True (the default), which means the
archive populates as a natural side-effect of the existing download flow —
the user does not need a separate "save to archive" action.
"""

from __future__ import annotations

import json
import sqlite3
from .db import now_iso
from typing import Any, Optional

from pydantic import BaseModel, Field


class ArchiveCreate(BaseModel):
    """Payload for inserting a finalized issue into the archive."""

    slug: str = Field(min_length=1, max_length=200)
    subject: Optional[str] = Field(default=None, max_length=500)
    filename: str = Field(min_length=1, max_length=300)
    payload: dict[str, Any] = Field(
        description="The NewsletterIssue payload that produced the .eml (JSON-serializable).",
    )
    eml_bytes: bytes = Field(description="The rendered .eml message bytes.")


class ArchiveSummary(BaseModel):
    """List-view item — no payload, no eml bytes, so the listing stays cheap."""

    id: int
    slug: str
    subject: Optional[str] = None
    filename: str
    size_bytes: int
    created_at: str


class ArchiveEntry(BaseModel):
    """Detail view of one archive row — includes payload but not the eml bytes.

    The eml bytes are streamed via ``GET /api/archive/{id}/download`` instead
    of being inlined into the JSON detail (they would just bloat the payload
    and the front-end fetches them through the binary endpoint anyway).
    """

    id: int
    slug: str
    subject: Optional[str] = None
    filename: str
    size_bytes: int
    created_at: str
    payload: dict[str, Any]





def _row_to_summary(row: sqlite3.Row) -> ArchiveSummary:
    return ArchiveSummary(
        id=row["id"],
        slug=row["slug"],
        subject=row["subject"],
        filename=row["filename"],
        size_bytes=int(row["size_bytes"]),
        created_at=row["created_at"],
    )


def _row_to_entry(row: sqlite3.Row) -> ArchiveEntry:
    return ArchiveEntry(
        id=row["id"],
        slug=row["slug"],
        subject=row["subject"],
        filename=row["filename"],
        size_bytes=int(row["size_bytes"]),
        created_at=row["created_at"],
        payload=json.loads(row["payload"]),
    )


def list_archive(conn: sqlite3.Connection) -> list[ArchiveSummary]:
    """Return all archive rows ordered by ``created_at`` descending."""
    rows = conn.execute(
        """
        SELECT id, slug, subject, filename,
               LENGTH(eml_bytes) AS size_bytes,
               created_at
        FROM archive
        ORDER BY created_at DESC, id DESC
        """
    ).fetchall()
    return [_row_to_summary(r) for r in rows]


def get_archive(conn: sqlite3.Connection, archive_id: int) -> Optional[ArchiveEntry]:
    row = conn.execute(
        """
        SELECT id, slug, subject, filename, payload,
               LENGTH(eml_bytes) AS size_bytes,
               created_at
        FROM archive
        WHERE id = ?
        """,
        (archive_id,),
    ).fetchone()
    return _row_to_entry(row) if row else None


def get_archive_eml(
    conn: sqlite3.Connection, archive_id: int
) -> Optional[tuple[str, bytes]]:
    """Return ``(filename, eml_bytes)`` for re-download, or ``None`` if absent."""
    row = conn.execute(
        "SELECT filename, eml_bytes FROM archive WHERE id = ?",
        (archive_id,),
    ).fetchone()
    if row is None:
        return None
    return row["filename"], bytes(row["eml_bytes"])


def create_archive(conn: sqlite3.Connection, data: ArchiveCreate) -> ArchiveSummary:
    """Insert a finalized issue. Returns the persisted summary (no payload)."""
    now = now_iso()
    payload_json = json.dumps(data.payload, ensure_ascii=False)
    cur = conn.execute(
        """
        INSERT INTO archive (slug, subject, filename, payload, eml_bytes, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (data.slug, data.subject, data.filename, payload_json, data.eml_bytes, now),
    )
    row = conn.execute(
        """
        SELECT id, slug, subject, filename,
               LENGTH(eml_bytes) AS size_bytes,
               created_at
        FROM archive
        WHERE id = ?
        """,
        (cur.lastrowid,),
    ).fetchone()
    assert row is not None
    return _row_to_summary(row)


def delete_archive(conn: sqlite3.Connection, archive_id: int) -> bool:
    """Delete by id. Returns ``True`` on delete, ``False`` when row did not exist."""
    row = conn.execute(
        "SELECT 1 FROM archive WHERE id = ?", (archive_id,)
    ).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM archive WHERE id = ?", (archive_id,))
    return True
