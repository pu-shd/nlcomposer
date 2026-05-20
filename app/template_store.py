"""Issue templates — preset starting points for new newsletter drafts.

Stored in the ``templates`` SQLite table (``payload`` is the JSON shape of a
:class:`~app.models.NewsletterIssue`, possibly partial). The application
does not validate the payload against ``NewsletterIssue`` on save — drafts
can be incomplete — but the renderer will surface validation errors when
the user tries to preview or download.

Builtin templates are seeded on first ``init_db`` and re-applied on every
startup if their slot is missing. They are protected from deletion but
can be edited freely (``builtin`` is informational, not read-only).

Module name is ``template_store`` (not ``templates``) to avoid clashing
with the ``app/templates/`` Jinja directory.
"""

from __future__ import annotations

import json
import sqlite3
from .db import now_iso
from typing import Any, Optional

from pydantic import BaseModel, Field


class TemplateCreate(BaseModel):
    """Payload for ``POST /api/templates``."""

    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=500)
    payload: dict[str, Any] = Field(
        description="Partial NewsletterIssue payload — JSON-serializable.",
    )


class TemplateUpdate(BaseModel):
    """Payload for ``PUT /api/templates/{id}``. All fields optional."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=500)
    payload: Optional[dict[str, Any]] = None


class TemplateSummary(BaseModel):
    """List-view item — no payload, so the listing stays cheap."""

    id: int
    name: str
    description: Optional[str] = None
    builtin: bool = False
    updated_at: str


class Template(TemplateSummary):
    """Full template including the issue payload."""

    payload: dict[str, Any]





def _row_to_summary(row: sqlite3.Row) -> TemplateSummary:
    return TemplateSummary(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        builtin=bool(row["builtin"]),
        updated_at=row["updated_at"],
    )


def _row_to_template(row: sqlite3.Row) -> Template:
    return Template(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        builtin=bool(row["builtin"]),
        updated_at=row["updated_at"],
        payload=json.loads(row["payload"]),
    )


def list_templates(conn: sqlite3.Connection) -> list[TemplateSummary]:
    """Return all templates, builtins first then by ``updated_at`` desc."""
    rows = conn.execute(
        """
        SELECT id, name, description, builtin, updated_at
        FROM templates
        ORDER BY builtin DESC, updated_at DESC
        """
    ).fetchall()
    return [_row_to_summary(r) for r in rows]


def get_template(conn: sqlite3.Connection, template_id: int) -> Optional[Template]:
    row = conn.execute(
        "SELECT * FROM templates WHERE id = ?", (template_id,)
    ).fetchone()
    return _row_to_template(row) if row else None


class TemplateNameConflictError(ValueError):
    """Raised when a name collides with an existing template."""


class BuiltinDeleteError(ValueError):
    """Raised when a delete is attempted on a builtin template."""


def create_template(
    conn: sqlite3.Connection, data: TemplateCreate, *, builtin: bool = False
) -> Template:
    """Insert a new template. Raises ``TemplateNameConflictError`` on duplicate name."""
    now = now_iso()
    payload_json = json.dumps(data.payload, ensure_ascii=False)
    try:
        cur = conn.execute(
            """
            INSERT INTO templates (name, description, payload, builtin, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                data.name,
                data.description,
                payload_json,
                1 if builtin else 0,
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError as e:
        if "templates.name" in str(e) or "UNIQUE" in str(e).upper():
            raise TemplateNameConflictError(
                f"A template named {data.name!r} already exists."
            ) from e
        raise
    row = conn.execute(
        "SELECT * FROM templates WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    assert row is not None
    return _row_to_template(row)


def update_template(
    conn: sqlite3.Connection, template_id: int, data: TemplateUpdate
) -> Optional[Template]:
    """Patch an existing template. Returns ``None`` when not found."""
    existing = get_template(conn, template_id)
    if existing is None:
        return None
    new_name = data.name if data.name is not None else existing.name
    new_desc = data.description if data.description is not None else existing.description
    new_payload = data.payload if data.payload is not None else existing.payload
    payload_json = json.dumps(new_payload, ensure_ascii=False)
    try:
        conn.execute(
            """
            UPDATE templates
               SET name = ?, description = ?, payload = ?, updated_at = ?
             WHERE id = ?
            """,
            (new_name, new_desc, payload_json, now_iso(), template_id),
        )
    except sqlite3.IntegrityError as e:
        if "templates.name" in str(e) or "UNIQUE" in str(e).upper():
            raise TemplateNameConflictError(
                f"A template named {new_name!r} already exists."
            ) from e
        raise
    return get_template(conn, template_id)


def delete_template(conn: sqlite3.Connection, template_id: int) -> bool:
    """Delete by id. Refuses to delete a builtin template.

    Returns ``True`` on delete, ``False`` when the row did not exist.
    Raises :class:`BuiltinDeleteError` for builtins.
    """
    row = conn.execute(
        "SELECT builtin FROM templates WHERE id = ?", (template_id,)
    ).fetchone()
    if row is None:
        return False
    if row["builtin"]:
        raise BuiltinDeleteError("Cannot delete a built-in template.")
    conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
    return True


# ───────── Builtin seed ──────────────────────────────────────────────


_DEFAULT_TEMPLATE_NAME = "SmartDrivingCars Default"
_DEFAULT_TEMPLATE_DESCRIPTION = (
    "Empty starter shaped like a typical SmartDrivingCars issue: header, "
    "envelope, podcast section, sponsor/conference static blocks."
)


def _default_template_payload() -> dict[str, Any]:
    """The starter payload — a partial NewsletterIssue, no articles, no date.

    Numeric fields (``issue_year``, ``issue_number``) are present so the
    composer's number inputs land on a sensible value; the user is
    expected to bump them per issue. ``issue_date`` is omitted on purpose
    so the front-end's "today" seed wins on first load.
    """
    return {
        "issue_year": 14,
        "issue_number": 1,
        "issue_title": "NewIssue",
        "from_name": "Newsletter Editor",
        "from_email": "editor@example.edu",
        "to_email": "subscribers@example.edu",
        "articles": [],
        "static_blocks": [
            {
                "title": "The Real Case for Driverless Mobility",
                "html": (
                    "<p>Narrated by Fred Fishkin, available now on "
                    '<a href="https://www.audible.com/">Audible</a>.</p>'
                ),
                "enabled": True,
            },
            {
                "title": "2026 Automated Transportation Symposium (ATS)",
                "html": (
                    "<p>July 27-30, 2026, San Diego, CA. "
                    '<a href="https://www.cvent.com/">Call for papers</a>.</p>'
                ),
                "enabled": True,
            },
        ],
        "highlights": [],
        "unsubscribe_url": "https://newsletter.example.edu/newsletter",
    }


def seed_default_templates(conn: sqlite3.Connection) -> None:
    """Idempotent: insert the built-in starter if it isn't already present.

    Matches by ``name`` (which is UNIQUE in the schema). Does not touch
    user edits to an existing builtin row — only fills in a missing slot.
    """
    row = conn.execute(
        "SELECT id FROM templates WHERE name = ?", (_DEFAULT_TEMPLATE_NAME,)
    ).fetchone()
    if row is not None:
        return
    create_template(
        conn,
        TemplateCreate(
            name=_DEFAULT_TEMPLATE_NAME,
            description=_DEFAULT_TEMPLATE_DESCRIPTION,
            payload=_default_template_payload(),
        ),
        builtin=True,
    )
    conn.commit()
