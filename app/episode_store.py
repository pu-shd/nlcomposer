"""Data access layer for YouTube channels and episodes.

Channels are managed by handle (e.g. @examplechannel) and their recent
uploads are cached in the ``youtube_episodes`` table.
"""

from __future__ import annotations

import json
import sqlite3
from .db import now_iso
from typing import Optional

from .models import YouTubeChannel, YouTubeEpisode





def _row_to_channel(row: sqlite3.Row) -> YouTubeChannel:
    return YouTubeChannel(
        id=row["id"],
        handle=row["handle"],
        name=row["name"],
        channel_id=row["channel_id"],
        uploads_playlist_id=row["uploads_playlist_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_refresh_at=row["last_refresh_at"] if "last_refresh_at" in row.keys() else None,
        last_refresh_status=row["last_refresh_status"] if "last_refresh_status" in row.keys() else None,
        last_refresh_error=row["last_refresh_error"] if "last_refresh_error" in row.keys() else None,
        last_refresh_added_count=row["last_refresh_added_count"] if "last_refresh_added_count" in row.keys() else None,

    )


def _row_to_episode(row: sqlite3.Row) -> YouTubeEpisode:
    return YouTubeEpisode(
        id=row["id"],
        channel_id=row["channel_id"],
        video_id=row["video_id"],
        title=row["title"],
        description=row["description"],
        published_at=row["published_at"],
        thumbnail_url=row["thumbnail_url"],
        payload=json.loads(row["payload"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_channels(conn: sqlite3.Connection) -> list[YouTubeChannel]:
    rows = conn.execute(
        "SELECT * FROM youtube_channels ORDER BY name ASC"
    ).fetchall()
    return [_row_to_channel(r) for r in rows]


def get_channel(conn: sqlite3.Connection, channel_id: int) -> Optional[YouTubeChannel]:
    row = conn.execute(
        "SELECT * FROM youtube_channels WHERE id = ?", (channel_id,)
    ).fetchone()
    return _row_to_channel(row) if row else None


def get_channel_by_handle(conn: sqlite3.Connection, handle: str) -> Optional[YouTubeChannel]:
    row = conn.execute(
        "SELECT * FROM youtube_channels WHERE handle = ?", (handle,)
    ).fetchone()
    return _row_to_channel(row) if row else None


def create_channel(conn: sqlite3.Connection, handle: str, name: str, yt_channel_id: str, uploads_playlist_id: str) -> YouTubeChannel:
    now = now_iso()
    cur = conn.execute(
        """
        INSERT INTO youtube_channels (handle, name, channel_id, uploads_playlist_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (handle, name, yt_channel_id, uploads_playlist_id, now, now),
    )
    return get_channel(conn, cur.lastrowid)


def delete_channel(conn: sqlite3.Connection, channel_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM youtube_channels WHERE id = ?", (channel_id,)
    ).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM youtube_channels WHERE id = ?", (channel_id,))
    return True


def list_episodes(conn: sqlite3.Connection, channel_id: int) -> list[YouTubeEpisode]:
    rows = conn.execute(
        """
        SELECT * FROM youtube_episodes
         WHERE channel_id = ?
         ORDER BY published_at DESC
        """,
        (channel_id,),
    ).fetchall()
    return [_row_to_episode(r) for r in rows]


def get_episode(conn: sqlite3.Connection, episode_id: int) -> Optional[YouTubeEpisode]:
    row = conn.execute(
        "SELECT * FROM youtube_episodes WHERE id = ?", (episode_id,)
    ).fetchone()
    return _row_to_episode(row) if row else None


def upsert_episode(
    conn: sqlite3.Connection,
    channel_id: int,
    video_id: str,
    title: str,
    description: str,
    published_at: str,
    thumbnail_url: str,
    payload: dict,
) -> YouTubeEpisode:
    now = now_iso()
    payload_json = json.dumps(payload, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO youtube_episodes (
            channel_id, video_id, title, description, published_at,
            thumbnail_url, payload, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            channel_id   = excluded.channel_id,
            title        = excluded.title,
            description  = excluded.description,
            published_at = excluded.published_at,
            thumbnail_url = excluded.thumbnail_url,
            payload      = excluded.payload,
            updated_at   = excluded.updated_at
        """,
        (
            channel_id,
            video_id,
            title,
            description,
            published_at,
            thumbnail_url,
            payload_json,
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM youtube_episodes WHERE video_id = ?", (video_id,)
    ).fetchone()
    return _row_to_episode(row)


def clear_channel_episodes(conn: sqlite3.Connection, channel_id: int) -> None:
    conn.execute("DELETE FROM youtube_episodes WHERE channel_id = ?", (channel_id,))


def update_channel_refresh_stats(
    conn: sqlite3.Connection,
    channel_id: int,
    status: str,
    error: Optional[str] = None,
    added_count: int = 0
) -> None:
    now = now_iso()
    conn.execute(
        """
        UPDATE youtube_channels
           SET last_refresh_at = ?,
               last_refresh_status = ?,
               last_refresh_error = ?,
               last_refresh_added_count = ?,
               updated_at = ?
         WHERE id = ?
        """,
        (now, status, error, added_count, now, channel_id),
    )

