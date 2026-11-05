"""Tests for the templates store + /api/templates endpoints + seed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import app
from app.template_store import (
    BuiltinDeleteError,
    TemplateCreate,
    TemplateNameConflictError,
    TemplateUpdate,
    create_template,
    delete_template,
    get_template,
    list_templates,
    seed_default_templates,
    update_template,
)


# ───────── Store-level CRUD ──────────────────────────────────────────


def test_create_and_get_template():
    conn = connect()
    init_db(conn)
    try:
        t = create_template(
            conn,
            TemplateCreate(
                name="Test",
                description="hi",
                payload={"issue_year": 14, "articles": []},
            ),
        )
        conn.commit()
        loaded = get_template(conn, t.id)
    finally:
        conn.close()
    assert loaded is not None
    assert loaded.name == "Test"
    assert loaded.builtin is False
    assert loaded.payload == {"issue_year": 14, "articles": []}


def test_list_orders_builtins_first():
    conn = connect()
    init_db(conn)
    try:
        # User templates first
        create_template(conn, TemplateCreate(name="UserA", payload={}))
        create_template(conn, TemplateCreate(name="UserB", payload={}))
        # Builtin added last
        create_template(
            conn, TemplateCreate(name="Built", payload={}), builtin=True
        )
        conn.commit()
        rows = list_templates(conn)
    finally:
        conn.close()
    assert rows[0].name == "Built"
    assert rows[0].builtin is True
    assert {r.name for r in rows[1:]} == {"UserA", "UserB"}


def test_create_unique_name_constraint():
    conn = connect()
    init_db(conn)
    try:
        create_template(conn, TemplateCreate(name="Dup", payload={}))
        conn.commit()
        with pytest.raises(TemplateNameConflictError):
            create_template(conn, TemplateCreate(name="Dup", payload={}))
    finally:
        conn.close()


def test_update_template_patches_only_provided_fields():
    conn = connect()
    init_db(conn)
    try:
        t = create_template(
            conn,
            TemplateCreate(
                name="Old", description="d", payload={"a": 1}
            ),
        )
        conn.commit()
        # Patch only the name
        updated = update_template(
            conn, t.id, TemplateUpdate(name="New")
        )
        conn.commit()
    finally:
        conn.close()
    assert updated is not None
    assert updated.name == "New"
    assert updated.description == "d"
    assert updated.payload == {"a": 1}


def test_update_returns_none_for_unknown_id():
    conn = connect()
    init_db(conn)
    try:
        result = update_template(conn, 99999, TemplateUpdate(name="x"))
    finally:
        conn.close()
    assert result is None


def test_delete_user_template():
    conn = connect()
    init_db(conn)
    try:
        t = create_template(conn, TemplateCreate(name="Tmp", payload={}))
        conn.commit()
        assert delete_template(conn, t.id) is True
        assert get_template(conn, t.id) is None
    finally:
        conn.close()


def test_delete_builtin_refused():
    conn = connect()
    init_db(conn)
    try:
        t = create_template(
            conn, TemplateCreate(name="Sys", payload={}), builtin=True
        )
        conn.commit()
        with pytest.raises(BuiltinDeleteError):
            delete_template(conn, t.id)
        assert get_template(conn, t.id) is not None
    finally:
        conn.close()


def test_delete_unknown_returns_false():
    conn = connect()
    init_db(conn)
    try:
        assert delete_template(conn, 99999) is False
    finally:
        conn.close()


def test_seed_is_idempotent():
    conn = connect()
    init_db(conn)
    try:
        seed_default_templates(conn)
        seed_default_templates(conn)
        rows = list_templates(conn)
    finally:
        conn.close()
    builtins = [r for r in rows if r.builtin]
    assert len(builtins) == 1
    assert builtins[0].name == "Default"


# ───────── HTTP endpoints ────────────────────────────────────────────


def test_list_includes_seeded_default(client: TestClient):
    res = client.get("/api/templates")
    assert res.status_code == 200
    body = res.json()
    names = [t["name"] for t in body]
    assert "Default" in names
    seeded = next(t for t in body if t["name"] == "Default")
    assert seeded["builtin"] is True


def test_get_returns_full_payload(client: TestClient):
    listing = client.get("/api/templates").json()
    seeded = next(t for t in listing if t["builtin"])
    res = client.get(f"/api/templates/{seeded['id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "Default"
    assert isinstance(body["payload"], dict)
    assert "static_blocks" in body["payload"]


def test_get_unknown_returns_404(client: TestClient):
    res = client.get("/api/templates/99999")
    assert res.status_code == 404


def test_create_via_post(client: TestClient):
    body = {
        "name": "My Custom",
        "description": "personal preset",
        "payload": {"issue_year": 14, "issue_number": 5, "issue_title": "Custom"},
    }
    res = client.post("/api/templates", json=body)
    assert res.status_code == 201, res.text
    created = res.json()
    assert created["id"] > 0
    assert created["builtin"] is False
    assert created["name"] == "My Custom"
    assert created["payload"]["issue_title"] == "Custom"


def test_create_duplicate_name_returns_409(client: TestClient):
    payload = {"name": "DupCustom", "payload": {}}
    assert client.post("/api/templates", json=payload).status_code == 201
    res = client.post("/api/templates", json=payload)
    assert res.status_code == 409


def test_create_collision_with_builtin_returns_409(client: TestClient):
    res = client.post(
        "/api/templates",
        json={"name": "Default", "payload": {}},
    )
    assert res.status_code == 409


def test_put_updates_fields(client: TestClient):
    created = client.post(
        "/api/templates",
        json={"name": "EditMe", "payload": {"issue_year": 14}},
    ).json()
    res = client.put(
        f"/api/templates/{created['id']}",
        json={
            "description": "updated",
            "payload": {"issue_year": 15, "issue_title": "x"},
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["description"] == "updated"
    assert body["payload"]["issue_year"] == 15
    assert body["name"] == "EditMe"  # unchanged


def test_put_unknown_returns_404(client: TestClient):
    res = client.put("/api/templates/99999", json={"name": "x"})
    assert res.status_code == 404


def test_delete_user_returns_204(client: TestClient):
    created = client.post(
        "/api/templates", json={"name": "DeleteMe", "payload": {}}
    ).json()
    res = client.delete(f"/api/templates/{created['id']}")
    assert res.status_code == 204
    assert client.get(f"/api/templates/{created['id']}").status_code == 404


def test_delete_builtin_returns_409(client: TestClient):
    listing = client.get("/api/templates").json()
    seeded = next(t for t in listing if t["builtin"])
    res = client.delete(f"/api/templates/{seeded['id']}")
    assert res.status_code == 409


def test_delete_unknown_returns_404(client: TestClient):
    res = client.delete("/api/templates/99999")
    assert res.status_code == 404


def test_create_rejects_missing_name(client: TestClient):
    res = client.post("/api/templates", json={"payload": {}})
    assert res.status_code == 422


def test_create_rejects_empty_name(client: TestClient):
    res = client.post("/api/templates", json={"name": "", "payload": {}})
    assert res.status_code == 422


# ───────── UI wiring (smoke checks) ──────────────────────────────────


def test_index_includes_templates_button_and_dialog(client: TestClient):
    body = client.get("/").text
    assert 'id="open-templates"' in body
    assert 'id="templates-dialog"' in body


def test_composer_js_calls_templates_endpoint(client: TestClient):
    js = client.get("/static/composer.js").text
    assert "/api/templates" in js
    assert "loadIssueIntoForm" in js
