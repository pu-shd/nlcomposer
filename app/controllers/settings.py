"""App-wide settings controller."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from ..db import get_db
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
    """Replace the stored settings with the supplied payload."""
    return save_settings(conn, settings)
