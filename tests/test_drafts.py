"""Tests for the drafts store + /api/drafts endpoints."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.draft_store import (
    DraftCreate,
    DraftUpdate,
    create_draft,
    delete_draft,
    get_draft,
    list_drafts,
    update_draft,
)
from app.main import app


@pytest.fixture
def client() -> TestClient:
    """Use the lifespan context manager so init_db runs on startup."""
    with TestClient(app) as c:
        yield c


# ───────── Store-level CRUD ──────────────────────────────────────────


def test_create_and_get_draft():
    conn = connect()
    init_db(conn)
    try:
        d = create_draft(
            conn,
            DraftCreate(name="WIP", payload={"issue_year": 14, "articles": []}),
        )
        conn.commit()
        loaded = get_draft(conn, d.id)
    finally:
        conn.close()
    assert loaded is not None
    assert loaded.name == "WIP"
    assert loaded.payload == {"issue_year": 14, "articles": []}
    assert loaded.created_at == loaded.updated_at


def test_list_orders_by_updated_desc():
    conn = connect()
    init_db(conn)
    try:
        a = create_draft(conn, DraftCreate(name="A", payload={"a": 1}))
        time.sleep(0.005)
        b = create_draft(conn, DraftCreate(name="B", payload={"b": 1}))
        time.sleep(0.005)
        # Touch A so it becomes the most-recently-updated
        update_draft(conn, a.id, DraftUpdate(payload={"a": 2}))
        conn.commit()
        rows = list_drafts(conn)
    finally:
        conn.close()
    assert [r.name for r in rows] == ["A", "B"]


def test_list_returns_empty_when_no_drafts():
    conn = connect()
    init_db(conn)
    try:
        rows = list_drafts(conn)
    finally:
        conn.close()
    assert rows == []


def test_duplicate_names_allowed():
    """Drafts (unlike templates) do not enforce a unique name constraint."""
    conn = connect()
    init_db(conn)
    try:
        d1 = create_draft(conn, DraftCreate(name="WIP", payload={"a": 1}))
        d2 = create_draft(conn, DraftCreate(name="WIP", payload={"a": 2}))
        conn.commit()
    finally:
        conn.close()
    assert d1.id != d2.id
    assert d1.name == d2.name == "WIP"


def test_update_patches_only_provided_fields():
    conn = connect()
    init_db(conn)
    try:
        d = create_draft(
            conn, DraftCreate(name="Old", payload={"a": 1})
        )
        conn.commit()
        time.sleep(0.005)
        updated = update_draft(conn, d.id, DraftUpdate(name="New"))
        conn.commit()
    finally:
        conn.close()
    assert updated is not None
    assert updated.name == "New"
    assert updated.payload == {"a": 1}
    assert updated.updated_at >= updated.created_at


def test_update_payload_only():
    conn = connect()
    init_db(conn)
    try:
        d = create_draft(conn, DraftCreate(name="x", payload={"a": 1}))
        conn.commit()
        updated = update_draft(
            conn, d.id, DraftUpdate(payload={"a": 2, "b": 3})
        )
        conn.commit()
    finally:
        conn.close()
    assert updated is not None
    assert updated.name == "x"
    assert updated.payload == {"a": 2, "b": 3}


def test_update_returns_none_for_unknown_id():
    conn = connect()
    init_db(conn)
    try:
        result = update_draft(conn, 99999, DraftUpdate(name="x"))
    finally:
        conn.close()
    assert result is None


def test_delete_draft():
    conn = connect()
    init_db(conn)
    try:
        d = create_draft(conn, DraftCreate(name="Tmp", payload={}))
        conn.commit()
        assert delete_draft(conn, d.id) is True
        assert get_draft(conn, d.id) is None
    finally:
        conn.close()


def test_delete_unknown_returns_false():
    conn = connect()
    init_db(conn)
    try:
        assert delete_draft(conn, 99999) is False
    finally:
        conn.close()


# ───────── HTTP endpoints ────────────────────────────────────────────


def test_list_initially_empty(client: TestClient):
    res = client.get("/api/drafts")
    assert res.status_code == 200
    assert res.json() == []


def test_create_via_post_returns_201(client: TestClient):
    body = {"name": "First", "payload": {"issue_year": 14}}
    res = client.post("/api/drafts", json=body)
    assert res.status_code == 201, res.text
    created = res.json()
    assert created["id"] > 0
    assert created["name"] == "First"
    assert created["payload"]["issue_year"] == 14
    assert "created_at" in created
    assert "updated_at" in created


def test_get_returns_full_payload(client: TestClient):
    created = client.post(
        "/api/drafts",
        json={"name": "Detailed", "payload": {"issue_year": 14, "issue_title": "x"}},
    ).json()
    res = client.get(f"/api/drafts/{created['id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "Detailed"
    assert body["payload"]["issue_title"] == "x"


def test_get_unknown_returns_404(client: TestClient):
    res = client.get("/api/drafts/99999")
    assert res.status_code == 404


def test_list_after_create(client: TestClient):
    client.post("/api/drafts", json={"name": "alpha", "payload": {}})
    client.post("/api/drafts", json={"name": "beta", "payload": {}})
    res = client.get("/api/drafts")
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 2
    assert {d["name"] for d in body} == {"alpha", "beta"}
    # Each summary entry omits the payload
    assert "payload" not in body[0]


def test_put_updates_fields(client: TestClient):
    created = client.post(
        "/api/drafts", json={"name": "EditMe", "payload": {"issue_year": 14}}
    ).json()
    res = client.put(
        f"/api/drafts/{created['id']}",
        json={"payload": {"issue_year": 15, "issue_title": "x"}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["payload"]["issue_year"] == 15
    assert body["name"] == "EditMe"


def test_put_unknown_returns_404(client: TestClient):
    res = client.put("/api/drafts/99999", json={"name": "x"})
    assert res.status_code == 404


def test_delete_returns_204(client: TestClient):
    created = client.post(
        "/api/drafts", json={"name": "DeleteMe", "payload": {}}
    ).json()
    res = client.delete(f"/api/drafts/{created['id']}")
    assert res.status_code == 204
    assert client.get(f"/api/drafts/{created['id']}").status_code == 404


def test_delete_unknown_returns_404(client: TestClient):
    res = client.delete("/api/drafts/99999")
    assert res.status_code == 404


def test_create_rejects_missing_name(client: TestClient):
    res = client.post("/api/drafts", json={"payload": {}})
    assert res.status_code == 422


def test_create_rejects_empty_name(client: TestClient):
    res = client.post("/api/drafts", json={"name": "", "payload": {}})
    assert res.status_code == 422


def test_round_trip_full_issue_payload(sample_issue, client: TestClient):
    """A complete NewsletterIssue should survive save → list → load."""
    issue_payload = sample_issue.model_dump(mode="json")
    created = client.post(
        "/api/drafts", json={"name": "Beijing WIP", "payload": issue_payload}
    ).json()

    fetched = client.get(f"/api/drafts/{created['id']}").json()
    assert fetched["payload"]["issue_year"] == sample_issue.issue_year
    assert fetched["payload"]["issue_title"] == sample_issue.issue_title
    assert len(fetched["payload"]["articles"]) == len(sample_issue.articles)


# ───────── UI wiring (smoke checks) ──────────────────────────────────


def test_index_includes_drafts_button_and_dialog(client: TestClient):
    body = client.get("/").text
    assert 'id="open-drafts"' in body
    assert 'id="drafts-dialog"' in body


def test_composer_js_calls_drafts_endpoint(client: TestClient):
    js = client.get("/static/composer.js").text
    assert "/api/drafts" in js
    assert "openDraftsDialog" in js
