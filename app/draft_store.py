"""Saved-but-incomplete newsletter drafts.

Drafts are persisted in the ``drafts`` SQLite table. Their ``payload`` is
the JSON shape of a :class:`~app.models.NewsletterIssue`, possibly partial
— the user can save mid-edit and come back to it later, so the payload is
not validated against the model on save. Names are not unique: the user
may have several drafts called "WIP" without a meaningful slug.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field


class DraftCreate(BaseModel):
    """Payload for ``POST /api/drafts``."""

    name: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(
        description="Partial NewsletterIssue payload — JSON-serializable.",
    )


class DraftUpdate(BaseModel):
    """Payload for ``PUT /api/drafts/{id}``. All fields optional."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    payload: Optional[dict[str, Any]] = None


class DraftSummary(BaseModel):
    """List-view item — no payload, so the listing stays cheap."""

    id: int
    name: str
    created_at: str
    updated_at: str


class Draft(DraftSummary):
    """Full draft including the issue payload."""

    payload: dict[str, Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_summary(row: sqlite3.Row) -> DraftSummary:
    return DraftSummary(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_draft(row: sqlite3.Row) -> Draft:
    return Draft(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        payload=json.loads(row["payload"]),
    )


def list_drafts(conn: sqlite3.Connection) -> list[DraftSummary]:
    """Return all drafts ordered by ``updated_at`` descending (most-recent first)."""
    rows = conn.execute(
        """
        SELECT id, name, created_at, updated_at
        FROM drafts
        ORDER BY updated_at DESC, id DESC
        """
    ).fetchall()
    return [_row_to_summary(r) for r in rows]


def get_draft(conn: sqlite3.Connection, draft_id: int) -> Optional[Draft]:
    row = conn.execute(
        "SELECT * FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    return _row_to_draft(row) if row else None


def create_draft(conn: sqlite3.Connection, data: DraftCreate) -> Draft:
    """Insert a new draft. Returns the persisted row."""
    now = _now_iso()
    payload_json = json.dumps(data.payload, ensure_ascii=False)
    cur = conn.execute(
        """
        INSERT INTO drafts (name, payload, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (data.name, payload_json, now, now),
    )
    row = conn.execute(
        "SELECT * FROM drafts WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    assert row is not None
    return _row_to_draft(row)


def update_draft(
    conn: sqlite3.Connection, draft_id: int, data: DraftUpdate
) -> Optional[Draft]:
    """Patch an existing draft. Returns ``None`` when not found."""
    existing = get_draft(conn, draft_id)
    if existing is None:
        return None
    new_name = data.name if data.name is not None else existing.name
    new_payload = data.payload if data.payload is not None else existing.payload
    payload_json = json.dumps(new_payload, ensure_ascii=False)
    conn.execute(
        """
        UPDATE drafts
           SET name = ?, payload = ?, updated_at = ?
         WHERE id = ?
        """,
        (new_name, payload_json, _now_iso(), draft_id),
    )
    return get_draft(conn, draft_id)


def delete_draft(conn: sqlite3.Connection, draft_id: int) -> bool:
    """Delete by id. Returns ``True`` on delete, ``False`` when row did not exist."""
    row = conn.execute(
        "SELECT 1 FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
    return True
