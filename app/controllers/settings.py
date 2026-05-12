"""App-wide settings controller."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db, init_db
from ..media_store import get_media
from ..settings import AppSettings, load_settings, save_settings


router = APIRouter(prefix="/api/settings")


def _validate_masthead_media(
    settings: AppSettings, conn: sqlite3.Connection
) -> None:
    """Enforce the same alt-text gate that Article logos and Static-block
    images use, but applied at save time on the masthead.

    Saved settings with ``masthead_logo_media_id`` set must point at a
    real Logos & Media item whose Image description is non-empty.
    Anything else is a 422 with an actionable detail message — the
    Settings dialog renders ``detail`` verbatim so the user knows what
    to fix without poking the dev tools.
    """
    media_id = settings.masthead_logo_media_id
    if media_id is None:
        return
    item = get_media(conn, media_id)
    if item is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Masthead logo references Logos & Media item #{media_id} "
                "but that item no longer exists. Open Logos & Media and "
                "either re-upload the logo or clear the reference in "
                "Settings."
            ),
        )
    if not (item.alt_text or "").strip():
        raise HTTPException(
            status_code=422,
            detail=(
                "Masthead logo is missing an Image description. Open "
                "Logos & Media, fill in the description for the uploaded "
                "logo, and try Save again."
            ),
        )


@router.get("", response_model=AppSettings)
def get_settings(conn: sqlite3.Connection = Depends(get_db)) -> AppSettings:
    """Return current app-wide settings, or built-in defaults if unset."""
    return load_settings(conn)


@router.put("", response_model=AppSettings)
def put_settings(
    settings: AppSettings,
    conn: sqlite3.Connection = Depends(get_db),
) -> AppSettings:
    """Replace the stored settings with the supplied payload.

    Before persisting we run the masthead-logo alt-text gate (parallel
    to what the in-newsletter image flow already enforces) and surface
    a 422 with a directly-actionable detail if the upload still needs a
    description. The schema self-heal below catches the narrow set of
    OperationalErrors that surface when the SQLite schema is missing
    (typically right after a ``wipe-data`` step before the container
    has fully come back up); one-shot ``init_db`` + retry recovers the
    operator's click instead of returning a bare 500. Failures past
    the retry are re-raised with a 503 + descriptive detail so the SPA
    can show the underlying message rather than ``Save 500: Internal
    Server Error``.
    """
    _validate_masthead_media(settings, conn)
    try:
        return save_settings(conn, settings)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            try:
                init_db(conn)
                return save_settings(conn, settings)
            except sqlite3.OperationalError as inner:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "SQLite schema is missing and re-initialization "
                        f"failed: {inner!s}. Check the container logs for "
                        "the lifespan init_db step."
                    ),
                )
        raise HTTPException(
            status_code=503,
            detail=f"SQLite error while saving settings: {exc!s}",
        )
