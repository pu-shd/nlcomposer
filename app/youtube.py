"""YouTube Data API v3 integration for fetching channel and episode metadata.
"""

from __future__ import annotations

import os
from typing import Optional, TypedDict
from urllib.parse import parse_qs, urlparse

import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .links import sanitize_url


class YouTubeVideo(TypedDict):
    video_id: str
    title: str
    description: str
    published_at: str
    thumbnail_url: str


def get_youtube_client():
    """Build a YouTube v3 client.

    Tries Application Default Credentials (ADC) first, then falls back to
    a YOUTUBE_API_KEY environment variable if set.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY")
    
    try:
        # Try ADC first
        credentials, project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/youtube.readonly"]
        )
        return build("youtube", "v3", credentials=credentials)
    except Exception as e:
        if api_key:
            return build("youtube", "v3", developerKey=api_key)
        raise RuntimeError("YouTube API is not configured or credentials are invalid.") from e


def extract_video_id(url: str) -> Optional[str]:
    """Pull the 11-ish-char video ID out of any common YouTube URL shape.

    Supports ``watch?v=…``, ``youtu.be/…``, ``shorts/…``, ``embed/…``,
    ``v/…``, and ``live/…``. Returns ``None`` if no ID is recognizable.
    """

    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return None
    host = (parsed.hostname or "").lower()
    if host == "youtu.be" or host.endswith(".youtu.be"):
        path_id = parsed.path.lstrip("/").split("/", 1)[0]
        return path_id or None
    if "youtube.com" not in host and "youtube-nocookie.com" not in host:
        return None
    if parsed.path == "/watch":
        qs = parse_qs(parsed.query)
        v = qs.get("v")
        if v and v[0]:
            return v[0]
    parts = parsed.path.lstrip("/").split("/", 2)
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "v", "live"}:
        return parts[1] or None
    return None


def validate_youtube_url(url: str) -> str:
    """Reject anything that doesn't yield a YouTube video ID.

    Also blocks file/ftp/loopback URLs the same way :func:`app.fetch.validate_fetch_url`
    does so the endpoint cannot be turned into an SSRF probe by accident.
    """

    cleaned = sanitize_url(url.strip()) or ""
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http(s) URLs are supported")
    if not parsed.netloc:
        raise ValueError("URL is missing a host")
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(
        ".localhost"
    ):
        raise ValueError("URL host is not allowed")
    if not extract_video_id(cleaned):
        raise ValueError("URL is not a recognizable YouTube video URL")
    return cleaned


def fetch_channel_by_handle(handle: str) -> Optional[dict]:
    """Find a channel by its handle (e.g. @smartdrivingcars)."""
    youtube = get_youtube_client()
    try:
        # Handles started with @ are relatively new in the API. 
        # forHandle works for the old custom URLs, but let's try it.
        # If it's a handle like @smartdrivingcars, we might need to search or use the handle parameter.
        request = youtube.channels().list(
            part="snippet,contentDetails",
            forHandle=handle
        )
        response = request.execute()
        
        if not response.get("items"):
            # Fallback: search for the channel if forHandle didn't work
            search_request = youtube.search().list(
                part="snippet",
                q=handle,
                type="channel",
                maxResults=1
            )
            search_response = search_request.execute()
            if not search_response.get("items"):
                return None
            
            channel_id = search_response["items"][0]["id"]["channelId"]
            request = youtube.channels().list(
                part="snippet,contentDetails",
                id=channel_id
            )
            response = request.execute()
            if not response.get("items"):
                return None

        item = response["items"][0]
        snippet = item["snippet"]
        return {
            "channel_id": item["id"],
            "name": snippet["title"],
            "handle": snippet.get("customUrl", handle),
            "uploads_playlist_id": item["contentDetails"]["relatedPlaylists"]["uploads"]
        }
    except HttpError as e:
        print(f"YouTube API Error: {e}")
        return None


def fetch_recent_uploads(playlist_id: str, max_results: int = 10) -> list[YouTubeVideo]:
    """Fetch the most recent videos from an uploads playlist."""
    youtube = get_youtube_client()
    try:
        request = youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=max_results
        )
        response = request.execute()
        
        videos = []
        for item in response.get("items", []):
            snippet = item["snippet"]
            video_id = snippet["resourceId"]["videoId"]
            videos.append({
                "video_id": video_id,
                "title": snippet["title"],
                "description": snippet["description"],
                "published_at": snippet["publishedAt"],
                "thumbnail_url": snippet.get("thumbnails", {}).get("high", {}).get("url") or \
                                snippet.get("thumbnails", {}).get("default", {}).get("url")
            })
        return videos
    except HttpError as e:
        print(f"YouTube API Error: {e}")
        return []


def fetch_video_details(video_id: str) -> Optional[YouTubeVideo]:
    """Fetch detailed metadata for a single video."""
    youtube = get_youtube_client()
    try:
        request = youtube.videos().list(
            part="snippet,contentDetails",
            id=video_id
        )
        response = request.execute()
        
        if not response.get("items"):
            return None
            
        item = response["items"][0]
        snippet = item["snippet"]
        return {
            "video_id": video_id,
            "title": snippet["title"],
            "description": snippet["description"],
            "published_at": snippet["publishedAt"],
            "thumbnail_url": snippet.get("thumbnails", {}).get("high", {}).get("url") or \
                            snippet.get("thumbnails", {}).get("default", {}).get("url")
        }
    except HttpError as e:
        print(f"YouTube API Error: {e}")
        return None
