"""API routes for managing YouTube channels and importing episodes.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..db import get_db
from ..episode_store import (
    create_channel,
    delete_channel,
    get_channel,
    get_channel_by_handle,
    list_channels,
    list_episodes,
    upsert_episode,
    clear_channel_episodes,
)
from ..models import YouTubeChannel, YouTubeEpisode
from ..youtube import fetch_channel_by_handle, fetch_recent_uploads

router = APIRouter(prefix="/api/episodes", tags=["Episodes"])


class ChannelAddRequest(BaseModel):
    handle: str


@router.get("/channels", response_model=list[YouTubeChannel])
def get_channels(conn: sqlite3.Connection = Depends(get_db)):
    """List all managed YouTube channels."""
    return list_channels(conn)


@router.post("/channels", response_model=YouTubeChannel)
def add_channel(
    data: ChannelAddRequest,
    conn: sqlite3.Connection = Depends(get_db)
):
    """Add a new YouTube channel by its handle or name."""
    query = data.handle.strip()
    
    # If it looks like a handle, ensure it has @
    if query.startswith("@") or " " not in query:
        handle = query if query.startswith("@") else "@" + query
    else:
        # It's likely a name, use as is
        handle = query
        
    existing = get_channel_by_handle(conn, handle)
    if existing:
        return existing
    
    try:
        info = fetch_channel_by_handle(handle)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    if not info:
        # Try without the @ if it was added
        if handle.startswith("@"):
            try:
                info = fetch_channel_by_handle(handle[1:])
            except RuntimeError as e:
                raise HTTPException(status_code=503, detail=str(e))
            
    if not info:
        raise HTTPException(status_code=404, detail=f"YouTube channel '{query}' not found.")
    
    # Use the actual handle from YouTube if returned
    final_handle = info.get("handle", handle)
    if not final_handle.startswith("@"):
        final_handle = "@" + final_handle

    channel = create_channel(
        conn,
        handle=final_handle,
        name=info["name"],
        yt_channel_id=info["channel_id"],
        uploads_playlist_id=info["uploads_playlist_id"]
    )
    
    # Auto-sync episodes after adding
    try:
        _sync_episodes(conn, channel, info["uploads_playlist_id"])
    except RuntimeError as e:
        # If sync fails but channel is created, we still return the channel
        # The user can retry syncing later, though this shouldn't happen if the first fetch worked.
        print(f"Warning: Auto-sync failed after adding channel: {e}")
    
    return channel


@router.get("/channels/{channel_id}", response_model=YouTubeChannel)
def get_channel_details(
    channel_id: int,
    conn: sqlite3.Connection = Depends(get_db)
):
    """Get details for a managed YouTube channel."""
    channel = get_channel(conn, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found.")
    return channel


@router.delete("/channels/{channel_id}")
def remove_channel(
    channel_id: int,
    conn: sqlite3.Connection = Depends(get_db)
):
    """Remove a managed YouTube channel."""
    if not delete_channel(conn, channel_id):
        raise HTTPException(status_code=404, detail="Channel not found.")
    return {"ok": True}


@router.get("/channels/{channel_id}/episodes", response_model=list[YouTubeEpisode])
def get_channel_episodes(
    channel_id: int,
    conn: sqlite3.Connection = Depends(get_db)
):
    """List cached episodes for a channel."""
    return list_episodes(conn, channel_id)


@router.post("/channels/{channel_id}/sync")
def sync_channel_episodes(
    channel_id: int,
    conn: sqlite3.Connection = Depends(get_db)
):
    """Fetch recent uploads from YouTube and update the local cache."""
    channel = get_channel(conn, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found.")
    
    playlist_id = channel.uploads_playlist_id
    if not playlist_id:
        try:
            info = fetch_channel_by_handle(channel.handle)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        if not info:
             raise HTTPException(status_code=404, detail="Could not fetch channel info from YouTube.")
        playlist_id = info["uploads_playlist_id"]
        # Update the channel with the playlist_id for next time
        conn.execute(
            "UPDATE youtube_channels SET uploads_playlist_id = ? WHERE id = ?",
            (playlist_id, channel_id)
        )

    try:
        count = _sync_episodes(conn, channel, playlist_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"count": count}


@router.post("/channels/{channel_id}/reset")
def reset_channel_episodes(
    channel_id: int,
    conn: sqlite3.Connection = Depends(get_db)
):
    """Clear local cache and re-sync from YouTube."""
    clear_channel_episodes(conn, channel_id)
    return sync_channel_episodes(channel_id, conn)


def _sync_episodes(conn: sqlite3.Connection, channel: YouTubeChannel, playlist_id: str) -> int:
    uploads = fetch_recent_uploads(playlist_id)
    for v in uploads:
        upsert_episode(
            conn,
            channel_id=channel.id,
            video_id=v["video_id"],
            title=v["title"],
            description=v["description"],
            published_at=v["published_at"],
            thumbnail_url=v["thumbnail_url"],
            payload=v
        )
    return len(uploads)
