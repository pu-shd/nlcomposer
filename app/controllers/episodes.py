"""API routes for managing YouTube channels and importing episodes.
"""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel

from ..db import get_db, session
from ..episode_store import (
    create_channel,
    delete_channel,
    get_channel,
    get_channel_by_handle,
    list_channels,
    list_episodes,
    upsert_episode,
    clear_channel_episodes,
    update_channel_refresh_stats,
)
from ..models import YouTubeChannel, YouTubeEpisode
from ..youtube import fetch_channel_by_handle, fetch_recent_uploads
from .utils import or_404


router = APIRouter(prefix="/api/episodes", tags=["Episodes"])

_syncing_channels = set()


def auto_refresh_channels_task(channel_ids: list[int]):
    """Background task to sync channel episodes, ensuring no concurrent operations."""
    for cid in channel_ids:
        if cid in _syncing_channels:
            continue
        _syncing_channels.add(cid)
        try:
            with session() as conn:
                channel = get_channel(conn, cid)
                if not channel:
                    continue

                # Double check stale condition inside background task
                if channel.last_refresh_at:
                    try:
                        last_refresh = datetime.fromisoformat(channel.last_refresh_at)
                        if last_refresh.tzinfo is None:
                            last_refresh = last_refresh.replace(tzinfo=timezone.utc)
                        elapsed = (datetime.now(timezone.utc) - last_refresh).total_seconds()
                        if elapsed < 3600:  # 1 hour
                            continue
                    except Exception:
                        pass

                playlist_id = channel.uploads_playlist_id
                if not playlist_id:
                    info = fetch_channel_by_handle(channel.handle)
                    if not info:
                        raise RuntimeError("Could not fetch channel info from YouTube.")
                    playlist_id = info["uploads_playlist_id"]
                    conn.execute(
                        "UPDATE youtube_channels SET uploads_playlist_id = ? WHERE id = ?",
                        (playlist_id, cid)
                    )

                count = _sync_episodes(conn, channel, playlist_id)
                update_channel_refresh_stats(conn, cid, status="success", added_count=count)
        except Exception as e:
            try:
                with session() as conn:
                    update_channel_refresh_stats(conn, cid, status="failed", error=str(e))
            except Exception as db_err:
                print(f"Error logging failed sync stats: {db_err}")
        finally:
            _syncing_channels.discard(cid)

class ChannelAddRequest(BaseModel):
    handle: str


@router.get("/channels", response_model=list[YouTubeChannel])
def get_channels(
    background_tasks: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_db)
):
    """List all managed YouTube channels."""
    channels = list_channels(conn)

    stale_ids = []
    now = datetime.now(timezone.utc)
    for c in channels:
        if c.id in _syncing_channels:
            continue

        is_stale = False
        if not c.last_refresh_at:
            is_stale = True
        else:
            try:
                last_refresh = datetime.fromisoformat(c.last_refresh_at)
                if last_refresh.tzinfo is None:
                    last_refresh = last_refresh.replace(tzinfo=timezone.utc)
                elapsed = (now - last_refresh).total_seconds()
                if elapsed >= 3600:  # 1 hour
                    is_stale = True
            except Exception:
                is_stale = True

        if is_stale:
            stale_ids.append(c.id)

    if stale_ids:
        background_tasks.add_task(auto_refresh_channels_task, stale_ids)

    return channels


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
    if channel.id is not None:
        _syncing_channels.add(channel.id)
        try:
            count = _sync_episodes(conn, channel, info["uploads_playlist_id"])
            update_channel_refresh_stats(conn, channel.id, status="success", added_count=count)
        except Exception as e:
            print(f"Warning: Auto-sync failed after adding channel: {e}")
            update_channel_refresh_stats(conn, channel.id, status="failed", error=str(e))
        finally:
            _syncing_channels.discard(channel.id)

    # Re-fetch channel to get updated refresh stats
    if channel.id is not None:
        updated_channel = get_channel(conn, channel.id)
        if updated_channel:
            return updated_channel
    
    return channel


@router.get("/channels/{channel_id}", response_model=YouTubeChannel)
def get_channel_details(
    channel_id: int,
    conn: sqlite3.Connection = Depends(get_db)
):
    """Get details for a managed YouTube channel."""
    return or_404(get_channel(conn, channel_id), "Channel")



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
    channel = or_404(get_channel(conn, channel_id), "Channel")

    if channel_id in _syncing_channels:
        raise HTTPException(status_code=409, detail="Channel is already syncing.")

    _syncing_channels.add(channel_id)
    try:
        playlist_id = channel.uploads_playlist_id
        if not playlist_id:
            try:
                info = fetch_channel_by_handle(channel.handle)
            except Exception as e:
                update_channel_refresh_stats(conn, channel_id, status="failed", error=str(e))
                conn.commit()
                raise HTTPException(status_code=503, detail=str(e))
            if not info:
                update_channel_refresh_stats(conn, channel_id, status="failed", error="Could not fetch channel info from YouTube.")
                conn.commit()
                raise HTTPException(status_code=404, detail="Could not fetch channel info from YouTube.")
            playlist_id = info["uploads_playlist_id"]
            conn.execute(
                "UPDATE youtube_channels SET uploads_playlist_id = ? WHERE id = ?",
                (playlist_id, channel_id)
            )

        try:
            count = _sync_episodes(conn, channel, playlist_id)
            update_channel_refresh_stats(conn, channel_id, status="success", added_count=count)
            conn.commit()
        except Exception as e:
            update_channel_refresh_stats(conn, channel_id, status="failed", error=str(e))
            conn.commit()
            raise HTTPException(status_code=503, detail=str(e))
    finally:
        _syncing_channels.discard(channel_id)

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
