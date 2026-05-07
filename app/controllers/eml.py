"""``.eml`` download controller.

Renders the issue, optionally archives it (per ``AppSettings``), and
streams the bytes back as a ``message/rfc822`` attachment.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Response

from ..archive_store import ArchiveCreate, create_archive
from ..db import get_db
from ..eml import build_eml, build_eml_filename
from ..models import NewsletterIssue
from ..settings import load_settings


router = APIRouter()


@router.post("/api/eml")
def download_eml(
    issue: NewsletterIssue,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    eml_bytes = build_eml(issue)
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
