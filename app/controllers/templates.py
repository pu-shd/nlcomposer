"""Issue-template CRUD controller."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Response

from ..db import get_db
from ..template_store import (
    BuiltinDeleteError,
    Template,
    TemplateCreate,
    TemplateNameConflictError,
    TemplateSummary,
    TemplateUpdate,
    create_template,
    delete_template,
    get_template,
    list_templates,
    update_template,
)
from .utils import or_404


router = APIRouter(prefix="/api/templates")


@router.get("", response_model=list[TemplateSummary])
def api_list_templates(
    conn: sqlite3.Connection = Depends(get_db),
) -> list[TemplateSummary]:
    """Return all templates (builtins first), without their payloads."""
    return list_templates(conn)


@router.get("/{template_id}", response_model=Template)
def api_get_template(
    template_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Template:
    return or_404(get_template(conn, template_id), "Template")


@router.post("", response_model=Template, status_code=201)
def api_create_template(
    body: TemplateCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> Template:
    try:
        return create_template(conn, body)
    except TemplateNameConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.put("/{template_id}", response_model=Template)
def api_update_template(
    template_id: int,
    body: TemplateUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> Template:
    try:
        updated = update_template(conn, template_id, body)
    except TemplateNameConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return updated


@router.delete("/{template_id}", status_code=204)
def api_delete_template(
    template_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    try:
        deleted = delete_template(conn, template_id)
    except BuiltinDeleteError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Template not found")
    return Response(status_code=204)
