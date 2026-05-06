"""SQLite persistence layer for the composer.

The composer stores everything beyond the live edit session in a single
SQLite database under ``SDC_DATA_DIR/app.db`` (default ``./data/app.db``).
Four tables back the four UI surfaces:

* ``settings``   — single-row JSON blob of app-wide defaults.
* ``drafts``     — saved-but-incomplete issues the user is still editing.
* ``templates``  — preset starting points for new issues.
* ``archive``    — finalized issues with their rendered ``.eml`` bytes.

Each table stores the issue/payload as JSON text so the schema stays stable
as ``NewsletterIssue`` evolves; the application is responsible for the JSON
shape on read.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_DEFAULT_DATA_DIRNAME = "data"
_DB_FILENAME = "app.db"


def get_data_dir() -> Path:
    """Return (and create) the directory that holds ``app.db``.

    Honors the ``SDC_DATA_DIR`` env var so the deployment target can mount
    a persistent volume (e.g. Azure Files) at an arbitrary path.
    """
    env = os.environ.get("SDC_DATA_DIR")
    path = Path(env).expanduser() if env else Path.cwd() / _DEFAULT_DATA_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def get_db_path() -> Path:
    return get_data_dir() / _DB_FILENAME


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with sensible defaults.

    * ``row_factory`` returns ``sqlite3.Row`` so callers can use dict-style
      access without writing positional unpacks.
    * ``foreign_keys`` is on by default.
    * ``journal_mode`` is WAL — gives concurrent readers while a writer
      holds the lock, which matters when the lifespan-init connection
      and the per-request connection overlap briefly at startup.
    * ``check_same_thread=False`` because FastAPI's threadpool may hand
      the same connection across threads when used as a dependency yield.
    """
    target = Path(path) if path is not None else get_db_path()
    conn = sqlite3.connect(
        str(target),
        check_same_thread=False,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    payload     TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_updated ON drafts(updated_at DESC);

CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    description TEXT,
    payload     TEXT    NOT NULL,
    builtin     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS archive (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT    NOT NULL,
    subject     TEXT,
    filename    TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    eml_bytes   BLOB    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_archive_created ON archive(created_at DESC);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they do not exist. Idempotent."""
    conn.executescript(_SCHEMA)
    conn.commit()


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """Context manager for a short-lived write session.

    Commits on clean exit; rolls back on exception; always closes.
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency yielding a per-request connection.

    Usage:
        @app.get("/api/...")
        def handler(conn: sqlite3.Connection = Depends(get_db)): ...
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
