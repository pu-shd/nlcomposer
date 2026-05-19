import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from app.main import app
from app.db import get_db

@patch("app.controllers.episodes.fetch_channel_by_handle")
@patch("app.controllers.episodes.fetch_recent_uploads")
def test_add_channel_and_sync(mock_uploads, mock_channel, client):
    # Mock channel info
    mock_channel.return_value = {
        "channel_id": "UC_mock_id",
        "name": "Mock Channel",
        "handle": "@mock",
        "uploads_playlist_id": "UU_mock_id"
    }
    
    # Mock uploads
    mock_uploads.return_value = [
        {
            "video_id": "vid1",
            "title": "Video 1",
            "description": "Desc 1",
            "published_at": "2024-05-19T10:00:00Z",
            "thumbnail_url": "http://thumb1"
        }
    ]
    
    # Add channel
    response = client.post("/api/episodes/channels", json={"handle": "@mock"})
    assert response.status_code == 200
    data = response.json()
    assert data["handle"] == "@mock"
    assert data["name"] == "Mock Channel"
    channel_id = data["id"]
    
    # Check episodes were auto-synced
    response = client.get(f"/api/episodes/channels/{channel_id}/episodes")
    assert response.status_code == 200
    episodes = response.json()
    assert len(episodes) == 1
    assert episodes[0]["video_id"] == "vid1"
    assert episodes[0]["title"] == "Video 1"

@patch("app.controllers.episodes.fetch_channel_by_handle")
def test_add_channel_not_found(mock_channel, client):
    mock_channel.return_value = None
    response = client.post("/api/episodes/channels", json={"handle": "@nonexistent"})
    assert response.status_code == 404

import os
from google.auth.exceptions import DefaultCredentialsError

def test_add_channel_api_not_configured(client):
    # If fetch_channel_by_handle raises RuntimeError, we expect 503
    with patch("app.controllers.episodes.fetch_channel_by_handle", side_effect=RuntimeError("YouTube API is not configured or credentials are invalid.")):
        response = client.post("/api/episodes/channels", json={"handle": "@mock"})
        assert response.status_code == 503
        assert "YouTube API is not configured" in response.json()["detail"]

def test_sync_channel_api_not_configured(client):
    # Add a channel first by mocking
    with patch("app.controllers.episodes.fetch_channel_by_handle") as mock_channel:
        with patch("app.controllers.episodes.fetch_recent_uploads") as mock_uploads:
            mock_channel.return_value = {
                "channel_id": "UC_mock_id",
                "name": "Mock Channel",
                "handle": "@mock",
                "uploads_playlist_id": "UU_mock_id"
            }
            mock_uploads.return_value = []
            response = client.post("/api/episodes/channels", json={"handle": "@mock"})
            assert response.status_code == 200
            channel_id = response.json()["id"]

    # Now simulate API failure on sync
    with patch("app.controllers.episodes._sync_episodes", side_effect=RuntimeError("YouTube API is not configured or credentials are invalid.")):
        response = client.post(f"/api/episodes/channels/{channel_id}/sync")
        assert response.status_code == 503
        assert "YouTube API is not configured" in response.json()["detail"]

def test_get_youtube_client_raises_runtime_error():
    from app.youtube import get_youtube_client
    
    # We clear YOUTUBE_API_KEY if it exists and mock google.auth.default to fail
    with patch.dict(os.environ, clear=True):
        if "YOUTUBE_API_KEY" in os.environ:
            del os.environ["YOUTUBE_API_KEY"]
            
        with patch("google.auth.default", side_effect=DefaultCredentialsError("No ADC found")):
            with pytest.raises(RuntimeError, match="YouTube API is not configured or credentials are invalid."):
                get_youtube_client()
