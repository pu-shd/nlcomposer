"""Tests for the SQLite persistence layer."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import connect, get_data_dir, get_db_path, init_db, session


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_get_data_dir_creates_missing_path(tmp_path, monkeypatch):
    target = tmp_path / "deeply" / "nested" / "data"
    monkeypatch.setenv("SDC_DATA_DIR", str(target))
    p = get_data_dir()
    assert p.is_dir()
    assert p == target.resolve()


def test_get_db_path_lives_under_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SDC_DATA_DIR", str(tmp_path / "data"))
    db = get_db_path()
    assert db.parent == (tmp_path / "data").resolve()
    assert db.name == "app.db"


def test_init_db_creates_all_tables():
    conn = connect()
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert {"settings", "drafts", "templates", "archive"}.issubset(names)
    conn.close()


def test_init_db_is_idempotent():
    conn = connect()
    init_db(conn)
    init_db(conn)
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    conn.close()
    assert len([r for r in rows if r["name"] in {"settings", "drafts", "templates", "archive"}]) == 4


def test_settings_enforces_single_row():
    conn = connect()
    init_db(conn)
    conn.execute(
        "INSERT INTO settings (id, payload, updated_at) VALUES (1, ?, ?)",
        ("{}", _now()),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO settings (id, payload, updated_at) VALUES (2, ?, ?)",
            ("{}", _now()),
        )
    conn.close()


def test_templates_name_is_unique():
    with session() as conn:
        init_db(conn)
        conn.execute(
            "INSERT INTO templates (name, payload, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("default", "{}", _now(), _now()),
        )
    with pytest.raises(sqlite3.IntegrityError):
        with session() as conn:
            conn.execute(
                "INSERT INTO templates (name, payload, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("default", "{}", _now(), _now()),
            )


def test_session_commits_on_clean_exit():
    with session() as conn:
        init_db(conn)
        conn.execute(
            "INSERT INTO drafts (name, payload, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("draft-A", json.dumps({"hello": "world"}), _now(), _now()),
        )

    with session() as conn:
        rows = conn.execute("SELECT name, payload FROM drafts").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "draft-A"
    assert json.loads(rows[0]["payload"]) == {"hello": "world"}


def test_session_rolls_back_on_exception():
    with session() as conn:
        init_db(conn)

    with pytest.raises(RuntimeError):
        with session() as conn:
            conn.execute(
                "INSERT INTO drafts (name, payload, created_at, updated_at) VALUES (?, ?, ?, ?)",
                ("ghost", "{}", _now(), _now()),
            )
            raise RuntimeError("boom")

    with session() as conn:
        rows = conn.execute("SELECT id FROM drafts").fetchall()
    assert rows == []


def test_archive_round_trips_eml_blob():
    eml_bytes = b"From: x@y\r\nSubject: t\r\n\r\nbody\r\n"
    payload = json.dumps({"issue_year": 14, "issue_number": 9})

    with session() as conn:
        init_db(conn)
        conn.execute(
            "INSERT INTO archive (slug, subject, filename, payload, eml_bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("14.9-Beijing", "subj", "fn.eml", payload, eml_bytes, _now()),
        )

    with session() as conn:
        row = conn.execute(
            "SELECT slug, subject, filename, payload, eml_bytes FROM archive"
        ).fetchone()
    assert row["slug"] == "14.9-Beijing"
    assert row["filename"] == "fn.eml"
    assert json.loads(row["payload"]) == {"issue_year": 14, "issue_number": 9}
    assert bytes(row["eml_bytes"]) == eml_bytes


def test_lifespan_creates_db_file_on_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("SDC_DATA_DIR", str(tmp_path / "lifespan-data"))
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200

    db_file = tmp_path / "lifespan-data" / "app.db"
    assert db_file.exists()

    conn = sqlite3.connect(str(db_file))
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {"settings", "drafts", "templates", "archive"}.issubset(names)
