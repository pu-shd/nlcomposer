"""``.eml`` download controller.

Renders the issue, optionally archives it (per ``AppSettings``), and
streams the bytes back as a ``message/rfc822`` attachment.

When the issue carries ``masthead_logo_media_id``, the cached bytes from
the Media Library are resolved here and passed through to ``build_eml``
as ``masthead_logo_bytes``. The HTML template prefers ``cid:`` over the
``/api/media/{id}/content`` preview URL, so the resulting message embeds
the logo inline rather than asking the recipient's mail client to fetch
it from the deployed app.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Response

from ..archive_store import ArchiveCreate, create_archive
from ..db import get_db
from ..eml import build_eml, build_eml_filename
from ..media_store import get_media, get_media_bytes
from ..models import NewsletterIssue
from ..settings import load_settings


router = APIRouter()


def _resolve_masthead_logo(
    issue: NewsletterIssue, conn: sqlite3.Connection
) -> tuple[NewsletterIssue, bytes | None, str]:
    """Resolve the Media Library masthead logo (if any) to inline bytes.

    Returns ``(issue_for_render, bytes_or_None, filename)``. The issue is
    returned unchanged when no resolution is needed; when the referenced
    media item exists but its bytes are not yet cached (a URL-sourced
    pending item, for instance), ``masthead_logo_media_id`` is stripped
    on a copy so the Jinja template falls through to the URL fallback
    instead of emitting an unresolvable ``/api/media/{id}/content`` src
    that no mail client could ever load.
    """
    media_id = issue.masthead_logo_media_id
    if media_id is None:
        return issue, None, "masthead.png"
    pair = get_media_bytes(conn, media_id)
    if pair is None:
        # Bytes not cached — drop the media_id reference so the template
        # picks the URL fallback (or text-h1 if there is no URL).
        return (
            issue.model_copy(update={"masthead_logo_media_id": None}),
            None,
            "masthead.png",
        )
    payload, content_type = pair
    item = get_media(conn, media_id)
    filename = (item.filename if item else None) or (
        "masthead.jpg" if content_type == "image/jpeg" else "masthead.png"
    )
    return issue, payload, filename


@router.post("/api/eml")
def download_eml(
    issue: NewsletterIssue,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    issue, masthead_bytes, masthead_filename = _resolve_masthead_logo(issue, conn)
    eml_bytes = build_eml(
        issue,
        masthead_logo_bytes=masthead_bytes,
        masthead_logo_filename=masthead_filename,
    )
    filename = build_eml_filename(issue)
    if load_settings(conn).auto_archive_on_download:
        create_archive(
            conn,
            ArchiveCreate(
                slug=issue.header_slug(),
                subject=issue.effective_subject(),
                filename=filename,
                payload=issue.model_dump(mode="json"),
                eml_bytes=eml_bytes,
            ),
        )
    return Response(
        content=eml_bytes,
        media_type="message/rfc822",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
