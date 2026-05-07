"""Controllers (the C in MVC).

Each module in this package exposes a single ``router: APIRouter`` that
groups the route handlers for one resource. ``app.main`` mounts them all
on the FastAPI application during startup.
"""

from . import (
    archive,
    drafts,
    eml,
    fetch,
    media,
    pages,
    preview,
    settings,
    templates,
)

__all__ = [
    "archive",
    "drafts",
    "eml",
    "fetch",
    "media",
    "pages",
    "preview",
    "settings",
    "templates",
]
