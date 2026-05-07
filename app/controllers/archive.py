"""Archived-issue list/get/download/delete controller."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Response

from ..archive_store import (
    ArchiveEntry,
    ArchiveSummary,
    delete_archive,
    get_archive,
    get_archive_eml,
    list_archive,
)
from ..db import get_db


router = APIRouter(prefix="/api/archive")


@router.get("", response_model=list[ArchiveSummary])
def api_list_archive(
    conn: sqlite3.Connection = Depends(get_db),
) -> list[ArchiveSummary]:
    """Return archived issues, most-recently-saved first, without payloads."""
    return list_archive(conn)


@router.get("/{archive_id}", response_model=ArchiveEntry)
def api_get_archive(
    archive_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> ArchiveEntry:
    entry = get_archive(conn, archive_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Archive entry not found")
    return entry


@router.get("/{archive_id}/download")
def api_download_archive(
    archive_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """Stream the stored ``.eml`` bytes back to the user as a download."""
    found = get_archive_eml(conn, archive_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Archive entry not found")
    filename, eml_bytes = found
    return Response(
        content=eml_bytes,
        media_type="message/rfc822",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/{archive_id}", status_code=204)
def api_delete_archive(
    archive_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    deleted = delete_archive(conn, archive_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Archive entry not found")
    return Response(status_code=204)
