"""SQLite persistence layer for the composer.

The composer stores everything beyond the live edit session in a single
SQLite database under ``SDC_DATA_DIR/app.db`` (default ``./data/app.db``).
Five tables back the UI surfaces:

* ``settings``   — single-row JSON blob of app-wide defaults.
* ``drafts``     — saved-but-incomplete issues the user is still editing.
* ``templates``  — preset starting points for new issues.
* ``archive``    — finalized issues with their rendered ``.eml`` bytes.
* ``media``      — Media Library: cached PNG/JPG bytes plus alt text used
  by Article logos and Static-block images. Items have either a source
  ``url`` (URL ingest) or only ``content_bytes`` (uploaded). ``status``
  tracks the fetch lifecycle (pending → ready/failed).

Each table stores the issue/payload as JSON text so the schema stays stable
as ``NewsletterIssue`` evolves; the application is responsible for the JSON
shape on read.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
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
    # Networked filesystems (Azure Files SMB, NFSv2/3) report
    # "database is locked" for SQLite's default POSIX advisory locks,
    # because their lock semantics are unreliable. SQLite ships an
    # alternate VFS, ``unix-dotfile``, that coordinates writers via a
    # sentinel ``<db>-journal`` dotfile in the same directory — any
    # filesystem that supports atomic create/delete works. It is safe
    # for single-writer workloads (we run min/max replicas = 1) and
    # is what SQLite's docs recommend for shared network volumes.
    #
    # Honor an ``SDC_SQLITE_VFS`` env var so deployments that mount
    # an Azure Files share at SDC_DATA_DIR set this to ``unix-dotfile``,
    # while local-dev / tests stay on the platform default and keep
    # the standard pytest fixtures working.
    vfs = os.environ.get("SDC_SQLITE_VFS", "").strip()
    if vfs:
        conn = sqlite3.connect(
            f"file:{target.as_posix()}?vfs={vfs}",
            uri=True,
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
    else:
        conn = sqlite3.connect(
            str(target),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL would also need shared-memory + locking that SMB lacks, and
    # the unix-dotfile VFS doesn't support it. Stay on the default
    # rollback journal. ``try/except`` so a misconfigured filesystem
    # still degrades gracefully instead of crashing the lifespan.
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
    except sqlite3.OperationalError:
        pass
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

CREATE TABLE IF NOT EXISTS media (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT    NOT NULL CHECK (kind IN ('logo', 'image')),
    url           TEXT    UNIQUE,
    filename      TEXT,
    content_type  TEXT,
    content_bytes BLOB,
    alt_text      TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'ready', 'failed')),
    error         TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_updated ON media(updated_at DESC);

CREATE TABLE IF NOT EXISTS youtube_channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    handle      TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    channel_id  TEXT    NOT NULL UNIQUE,
    uploads_playlist_id TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    last_refresh_at TEXT,
    last_refresh_status TEXT,
    last_refresh_error TEXT,
    last_refresh_added_count INTEGER
);

CREATE TABLE IF NOT EXISTS youtube_episodes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id        INTEGER NOT NULL REFERENCES youtube_channels(id) ON DELETE CASCADE,
    video_id          TEXT    NOT NULL UNIQUE,
    title             TEXT    NOT NULL,
    description       TEXT,
    published_at      TEXT    NOT NULL,
    thumbnail_url     TEXT,
    payload           TEXT    NOT NULL,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_yt_episodes_published ON youtube_episodes(published_at DESC);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they do not exist. Idempotent."""
    conn.executescript(_SCHEMA)
    
    # Check if the new columns exist in youtube_channels and add them if not
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(youtube_channels)")
    columns = [row[1] for row in cursor.fetchall()]
    if columns:  # table exists
        if "last_refresh_at" not in columns:
            conn.execute("ALTER TABLE youtube_channels ADD COLUMN last_refresh_at TEXT")
        if "last_refresh_status" not in columns:
            conn.execute("ALTER TABLE youtube_channels ADD COLUMN last_refresh_status TEXT")
        if "last_refresh_error" not in columns:
            conn.execute("ALTER TABLE youtube_channels ADD COLUMN last_refresh_error TEXT")
        if "last_refresh_added_count" not in columns:
            conn.execute("ALTER TABLE youtube_channels ADD COLUMN last_refresh_added_count INTEGER")
            
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


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()

