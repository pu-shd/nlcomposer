"""App-wide settings controller."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db, init_db
from ..settings import AppSettings, load_settings, save_settings


router = APIRouter(prefix="/api/settings")


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

    The endpoint catches the narrow set of OperationalErrors that
    surface when the SQLite schema is missing (typically right after a
    ``wipe-data`` step before the container has fully come back up, or
    a botched first-boot where ``init_db`` didn't run). We attempt a
    one-shot ``init_db`` + retry so the user's click recovers itself
    instead of returning a bare 500. Failures past the retry are
    re-raised with a 503 + descriptive detail so the SPA can show the
    underlying message rather than ``Save 500: Internal Server Error``.
    """
    try:
        return save_settings(conn, settings)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            # Self-heal: re-run the idempotent schema bootstrap and try
            # again. Anything still failing is escalated to 503.
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
