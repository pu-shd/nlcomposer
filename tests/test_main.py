"""Regression tests for the MVC composition root.

These tests pin the contract between ``app/main.py`` and the
``app/controllers/`` package so a future refactor of one piece can't
silently drop a route or skip a router include.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app import controllers
from app.main import app


# Pairs of (HTTP method, path) that the SPA + tests rely on. The path
# patterns mirror what FastAPI registers in ``app.routes`` (path params
# stay as ``{name}``).
EXPECTED_ROUTES: list[tuple[str, str]] = [
    ("GET", "/"),
    ("GET", "/healthz"),
    ("POST", "/api/preview/html"),
    ("POST", "/api/preview/text"),
    ("POST", "/api/eml"),
    ("GET", "/api/settings"),
    ("PUT", "/api/settings"),
    ("GET", "/api/templates"),
    ("POST", "/api/templates"),
    ("GET", "/api/templates/{template_id}"),
    ("PUT", "/api/templates/{template_id}"),
    ("DELETE", "/api/templates/{template_id}"),
    ("GET", "/api/drafts"),
    ("POST", "/api/drafts"),
    ("GET", "/api/drafts/{draft_id}"),
    ("PUT", "/api/drafts/{draft_id}"),
    ("DELETE", "/api/drafts/{draft_id}"),
    ("GET", "/api/archive"),
    ("GET", "/api/archive/{archive_id}"),
    ("GET", "/api/archive/{archive_id}/download"),
    ("DELETE", "/api/archive/{archive_id}"),
    ("GET", "/api/fetch/article"),
    ("GET", "/api/fetch/youtube"),
    ("GET", "/api/media"),
    ("POST", "/api/media"),
    ("POST", "/api/media/upload"),
    ("GET", "/api/media/{media_id}"),
    ("PUT", "/api/media/{media_id}"),
    ("DELETE", "/api/media/{media_id}"),
    ("GET", "/api/media/{media_id}/content"),
]


# Every controller module under app/controllers/ must expose a `router`.
CONTROLLER_MODULES = (
    "archive",
    "drafts",
    "eml",
    "fetch",
    "media",
    "pages",
    "preview",
    "settings",
    "templates",
)


def _registered_routes() -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for r in app.routes:
        # ``Mount`` (e.g. /static) has no .methods — skip.
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", None)
        if not path:
            continue
        for m in methods:
            if m == "HEAD":
                continue
            pairs.add((m, path))
    return pairs


def test_healthz_returns_ok(client: TestClient):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.text == "ok"


@pytest.mark.parametrize("method,path", EXPECTED_ROUTES)
def test_expected_route_is_mounted(method: str, path: str):
    """The MVC composition root must include every controller — if a
    router isn't included, the SPA breaks. Pin the contract."""
    assert (method, path) in _registered_routes(), (
        f"missing route on app: {method} {path}"
    )


@pytest.mark.parametrize("name", CONTROLLER_MODULES)
def test_controller_module_exposes_router(name: str):
    """Architectural invariant: every module in app/controllers/ exposes
    a ``router`` attribute typed as ``APIRouter``. main.py iterates this
    list so a missing attribute would be a runtime AttributeError."""
    mod = getattr(controllers, name)
    router = getattr(mod, "router", None)
    assert isinstance(router, APIRouter), (
        f"app.controllers.{name} must expose a `router: APIRouter`, got {router!r}"
    )


def test_main_does_not_register_route_handlers_directly():
    """Route handlers belong in controllers/, not main.py. Detect
    regression where someone tacks an ``@app.get`` directly onto the
    composition root by checking that every (method, path) pair on the
    app comes from one of the included controller routers."""
    controller_pairs: set[tuple[str, str]] = set()
    for name in CONTROLLER_MODULES:
        mod = getattr(controllers, name)
        for r in mod.router.routes:
            methods = getattr(r, "methods", None) or set()
            path = getattr(r, "path", None)
            if not path:
                continue
            for m in methods:
                if m == "HEAD":
                    continue
                controller_pairs.add((m, path))

    rogue: set[tuple[str, str]] = set()
    for method, path in _registered_routes():
        # /static is a Mount and has no methods — already filtered.
        # /openapi.json, /docs, /redoc, /docs/oauth2-redirect are
        # FastAPI built-ins, not handlers we own.
        if path in {"/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}:
            continue
        if (method, path) not in controller_pairs:
            rogue.add((method, path))

    assert not rogue, (
        "route(s) registered outside app/controllers/: " + ", ".join(
            sorted(f"{m} {p}" for m, p in rogue)
        )
    )
