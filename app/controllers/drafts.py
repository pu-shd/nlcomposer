"""Saved-draft CRUD controller."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Response

from ..db import get_db
from ..draft_store import (
    Draft,
    DraftCreate,
    DraftSummary,
    DraftUpdate,
    create_draft,
    delete_draft,
    get_draft,
    list_drafts,
    update_draft,
    get_latest_draft,
)
from .utils import or_404


router = APIRouter(prefix="/api/drafts")


@router.get("/latest", response_model=Draft)
def api_get_latest_draft(
    conn: sqlite3.Connection = Depends(get_db),
) -> Draft:
    return or_404(get_latest_draft(conn), "Latest draft")


@router.get("", response_model=list[DraftSummary])
def api_list_drafts(
    conn: sqlite3.Connection = Depends(get_db),
) -> list[DraftSummary]:
    """Return all drafts (most-recently-updated first), without their payloads."""
    return list_drafts(conn)


@router.get("/{draft_id}", response_model=Draft)
def api_get_draft(
    draft_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Draft:
    return or_404(get_draft(conn, draft_id), "Draft")


@router.post("", response_model=Draft, status_code=201)
def api_create_draft(
    body: DraftCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> Draft:
    return create_draft(conn, body)


@router.put("/{draft_id}", response_model=Draft)
def api_update_draft(
    draft_id: int,
    body: DraftUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> Draft:
    return or_404(update_draft(conn, draft_id, body), "Draft")


@router.delete("/{draft_id}", status_code=204)
def api_delete_draft(
    draft_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    deleted = delete_draft(conn, draft_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Draft not found")
    return Response(status_code=204)
