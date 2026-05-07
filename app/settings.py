"""App-wide defaults and behavior toggles persisted in the ``settings`` table.

The composer used to ship with hard-coded form defaults (``from_name``,
``to_email``, etc.). Once the SQLite layer landed, those moved here so the
user can edit them in one place and have every new draft start from those
values. The model also carries a few behavior toggles (``auto_archive_on_download``,
``preview_debounce_ms``) that live outside the ``NewsletterIssue`` shape.

Persistence shape: a single row in ``settings`` with ``id=1`` whose
``payload`` column is the JSON serialization of :class:`AppSettings`.
``load_settings`` always returns *something* — if no row has been written
yet, you get the model defaults — so callers never need a None check.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date as _date, datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from .models import SanitizedHttpUrl


class AppSettings(BaseModel):
    """User-editable defaults applied to every new issue draft.

    Fields mirror the hot zones of the form. Adding a new default? Pick a
    name that matches the corresponding ``NewsletterIssue`` field so the
    front-end can copy values across with no remapping table.
    """

    # Email envelope defaults
    from_name: str = "Newsletter Editor"
    from_email: str = "editor@example.edu"
    to_email: str = "editor@example.edu"
    reply_to: Optional[str] = None

    # Reusable issue chrome
    unsubscribe_url: Optional[SanitizedHttpUrl] = None
    masthead_logo_url: Optional[SanitizedHttpUrl] = None
    default_intro_html: Optional[str] = Field(
        default=None,
        description="Carry-forward intro paragraph(s) for new drafts. Raw HTML.",
    )
    default_footer_html: Optional[str] = Field(
        default=None,
        description="Carry-forward footer for new drafts. Raw HTML.",
    )

    # Issue numbering
    initial_publication_date: Optional[_date] = Field(
        default=_date(2013, 1, 1),
        description=(
            "Date the newsletter first launched ('Inception Date' in the "
            "settings UI). The composer derives an issue's enumerated "
            "year (e.g. '14th year') from "
            "(issue_date − initial_publication_date), so the user no "
            "longer enters a Year value per issue. Default of 2013-01-01 "
            "matches the historical SmartDrivingCars launch."
        ),
    )

    # Behavior toggles
    preview_debounce_ms: int = Field(
        default=400,
        ge=0,
        le=5000,
        description="Delay between keystroke and preview re-render.",
    )
    auto_archive_on_download: bool = Field(
        default=True,
        description="When True, every successful .eml download is also stored in the archive.",
    )
    include_read_more_link: bool = Field(
        default=True,
        description=(
            "Render the 'Read more' link/line under each article. Off if "
            "the editor prefers the single headline link as the only "
            "click target. Mirrored onto NewsletterIssue at build time."
        ),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_settings(conn: sqlite3.Connection) -> AppSettings:
    """Return the stored settings, or model defaults if no row exists."""
    row = conn.execute("SELECT payload FROM settings WHERE id = 1").fetchone()
    if row is None:
        return AppSettings()
    try:
        data = json.loads(row["payload"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return AppSettings()
    return AppSettings.model_validate(data)


def save_settings(conn: sqlite3.Connection, settings: AppSettings) -> AppSettings:
    """Upsert the single settings row. Returns the validated payload."""
    payload = settings.model_dump_json()
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO settings (id, payload, updated_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            payload    = excluded.payload,
            updated_at = excluded.updated_at
        """,
        (payload, now),
    )
    return settings
