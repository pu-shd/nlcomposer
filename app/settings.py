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
from datetime import date as _date
from typing import Optional

from .db import now_iso

from pydantic import BaseModel, Field

from .models import CollapsedHtml, SanitizedHttpUrl


class AppSettings(BaseModel):
    """User-editable defaults applied to every new issue draft.

    Fields mirror the hot zones of the form. Adding a new default? Pick a
    name that matches the corresponding ``NewsletterIssue`` field so the
    front-end can copy values across with no remapping table.
    """

    newsletter_name: str = ""

    # Optional podcast title (printed at the beginning of Podcast sections).
    # If populated, it is used instead of the newsletter name.
    podcast_title: Optional[str] = Field(
        default=None,
        description=(
            "Optional title for the podcast section. If populated, it is "
            "used instead of inserting the newsletter name at the beginning "
            "of Podcast sections."
        ),
    )

    # Email envelope defaults — all empty by default so a fresh install
    # doesn't ship a placeholder identity. The composer form pulls from
    # AppSettings at buildIssue() time; when these are empty the model
    # falls back to its own defaults so previews still render.
    from_name: str = ""
    from_email: str = ""
    to_email: str = ""
    reply_to: Optional[str] = None

    # Reusable issue chrome
    unsubscribe_url: Optional[SanitizedHttpUrl] = None
    masthead_logo_url: Optional[SanitizedHttpUrl] = None
    masthead_logo_media_id: Optional[int] = Field(
        default=None,
        description=(
            "Media Library id for an uploaded masthead logo. Mirrors onto "
            "NewsletterIssue.masthead_logo_media_id at build time. When "
            "both this and ``masthead_logo_url`` are set, the renderer "
            "prefers the uploaded item (which can be embedded inline via "
            "cid:) and falls back to the URL only if the media item is "
            "missing or not yet ready."
        ),
    )
    default_intro_html: CollapsedHtml = Field(
        default=None,
        description="Carry-forward intro paragraph(s) for new drafts. Raw HTML.",
    )
    default_footer_html: CollapsedHtml = Field(
        default=None,
        description="Carry-forward footer for new drafts. Raw HTML.",
    )

    # Issue numbering
    initial_publication_date: Optional[_date] = Field(
        default=None,
        description=(
            "Date the newsletter first launched ('Inception Date' in the "
            "settings UI). The composer derives an issue's enumerated "
            "year (e.g. '14th year') from "
            "(issue_date − initial_publication_date), so the user no "
            "longer enters a Year value per issue. Unset by default; a "
            "deployment supplies it via instance settings_defaults."
        ),
    )

    # Behavior toggles
    preview_debounce_ms: int = Field(
        default=400,
        ge=0,
        le=5000,
        description="Delay between keystroke and preview re-render.",
    )
    include_read_more_link: bool = Field(
        default=True,
        description=(
            "Render the 'Read more' link/line under each article. Off if "
            "the editor prefers the single headline link as the only "
            "click target. Mirrored onto NewsletterIssue at build time."
        ),
    )
    confirm_before_delete: bool = Field(
        default=True,
        description=(
            "Show a confirmation dialog before deleting drafts, "
            "templates, or archived issues. Users who trust their own "
            "click can disable this from the confirm dialog itself."
        ),
    )
    auto_archive_on_download: bool = Field(
        default=True,
        description="Automatically archive issues when downloaded.",
    )
    embed_all_media: bool = Field(
        default=True,
        description=(
            "When True, the renderer prefers embedded (cid:) attachments "
            "for Article Logos and Static-block images that are present "
            "in the Media Library, falling back to the source URL with a "
            "warning if the cached bytes are not yet available. When "
            "False, every media reference is rendered as the source URL "
            "regardless of cache state."
        ),
    )

    # ── Media Library upload gating ───────────────────────────────────
    # The composer is meant to run on a developer's laptop, but the
    # /api/media/upload surface still accepts arbitrary bytes from the
    # browser, so we gate it with three configurable knobs and a
    # magic-byte sniffer (see app.media_store.sniff_image_kind). Any
    # change here applies on the next request — no restart needed.
    media_upload_max_bytes: int = Field(
        default=5 * 1024 * 1024,
        ge=1024,                  # at least 1 KiB to avoid pathological 0
        le=64 * 1024 * 1024,      # cap at 64 MiB even for power users
        description=(
            "Hard cap on a single uploaded image. Returns 413 above. "
            "Defaults to 5 MiB — newsletter sponsor logos and banners "
            "should sit well under that."
        ),
    )
    media_upload_allowed_types: list[str] = Field(
        default_factory=lambda: ["image/png", "image/jpeg"],
        description=(
            "Content types the upload endpoint accepts. Each entry is "
            "matched against BOTH the client-declared Content-Type and a "
            "magic-byte sniff of the first bytes — a mismatch returns "
            "415. Adding 'image/gif' or 'image/webp' here also requires "
            "extending sniff_image_kind() to recognize those signatures."
        ),
    )
    media_upload_rate_per_minute: int = Field(
        default=20,
        ge=0,
        le=600,
        description=(
            "Rolling 60-second cap on uploads per client (keyed by "
            "remote IP). Set to 0 to disable rate limiting entirely; "
            "anything above the cap returns 429."
        ),
    )
    media_url_imports_enabled: bool = Field(
        default=True,
        description=(
            "When False, POST /api/media (URL register) returns 403 — "
            "useful for instances that should only host uploaded bytes "
            "and never reach back out to arbitrary remote hosts."
        ),
    )

    # ── Autosave loop ─────────────────────────────────────────────────
    # The composer SPA runs a background tick that periodically
    # captures the in-progress issue as a draft so a crashed tab /
    # accidental close / network blip doesn't burn an hour of editing.
    # The interval is configurable (so power users can dial it down on
    # slow links) and 0 disables the loop entirely. The actual save
    # uses the existing POST/PUT /api/drafts endpoints — no new
    # surface, just a client-side scheduler against them.
    autosave_interval_seconds: int = Field(
        default=45,
        ge=0,
        le=600,
        description=(
            "Seconds between autosave ticks (0 = disabled). The SPA "
            "only writes when the assembled NewsletterIssue has changed "
            "since the last save — idle ticks are a no-op against the "
            "DB. Defaults to 45s; raise this on slow links or when the "
            "draft table is being inspected concurrently."
        ),
    )
    load_last_draft_on_login: bool = Field(
        default=True,
        description="When True, the SPA fetches and loads the most recently updated draft on page load.",
    )





def load_settings(conn: sqlite3.Connection) -> AppSettings:
    """Return the effective settings.

    Precedence: engine defaults < the deployment's instance
    ``settings_defaults`` < the row saved from the Settings dialog. A saved
    row that predates a setting lacks its key, so the instance default
    fills it.
    """
    from .instance import get_instance_config

    defaults = dict(get_instance_config().settings_defaults)
    row = conn.execute("SELECT payload FROM settings WHERE id = 1").fetchone()
    if row is None:
        return AppSettings.model_validate(defaults)
    try:
        data = json.loads(row["payload"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return AppSettings.model_validate(defaults)
    return AppSettings.model_validate({**defaults, **data})


def save_settings(conn: sqlite3.Connection, settings: AppSettings) -> AppSettings:
    """Upsert the single settings row. Returns the validated payload."""
    payload = settings.model_dump_json()
    now = now_iso()
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
