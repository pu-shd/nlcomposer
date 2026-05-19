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
