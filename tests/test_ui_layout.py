"""Regression tests for UI layout and positioning.

These tests verify that critical CSS properties and DOM relationships
are present to prevent regressions in element positioning and overlap.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_layout_structure_and_scrolling(client: TestClient):
    """Verify that the body and layout are configured to prevent page-level scroll."""
    css = client.get("/static/composer.css").text
    
    # Body is a flex container and hides overflow
    assert "overflow: hidden" in css[css.index("html, body {"):]
    assert "display: flex" in css[css.index("body {"):]
    assert "flex-direction: column" in css[css.index("body {"):]

    # Layout fills the remaining height
    assert ".layout {" in css
    layout_block = css[css.index(".layout {"):]
    assert "flex: 1" in layout_block[:200]
    assert "min-height: 0" in layout_block[:200]


def test_article_card_overlap_protection(client: TestClient):
    """Verify CSS rules that prevent input fields from overlapping in article cards."""
    css = client.get("/static/composer.css").text
    
    # Grid hardening for children
    assert "#composer .article-card .grid-2 > *" in css
    assert "min-width: 0" in css[css.index("#composer .article-card .grid-2 > *"):]
    assert "min-height: 0" in css[css.index("#composer .article-card .grid-2 > *"):]

    # Rich-text container hardening
    assert "#composer .rt-field" in css
    assert "width: 100%" in css[css.index("#composer .rt-field"):]
    assert "box-sizing: border-box" in css[css.index("#composer .rt-field"):]
    
    # Toolbar wrapping
    assert ".ql-toolbar.ql-snow" in css
    assert "flex-wrap: wrap" in css[css.index(".ql-toolbar.ql-snow"):]


def test_add_section_fab_pinning(client: TestClient):
    """Verify that the '+' button is pinned to the pane viewport, not the scroll window.
    
    Uses a grid-stack approach where the form and the FAB occupy the same
    grid area, but the FAB is aligned to the corner.
    """
    css = client.get("/static/composer.css").text
    html = client.get("/").text

    # 1. HTML: FAB row must be a sibling of the composer form, not inside it.
    assert '<form id="composer"' in html
    assert '<div class="fab-row"' in html
    form_idx = html.index('<form id="composer"')
    form_end_idx = html.index('</form>', form_idx)
    fab_idx = html.index('<div class="fab-row"')
    assert fab_idx > form_end_idx, "FAB row should be outside the scrolling form"

    # 2. CSS: Verify the grid stack logic on the parent pane.
    assert ".form-pane" in css
    pane_block = css[css.index(".form-pane"):]
    assert "display: grid" in pane_block[:100]
    assert 'grid-template-areas: "stack"' in pane_block[:100]

    # 3. CSS: Verify the FAB is aligned to the corner of the stack.
    assert ".fab-row" in css
    fab_block = css[css.index(".fab-row"):]
    assert "grid-area: stack" in fab_block[:100]
    assert "align-self: end" in fab_block[:100]
    assert "justify-self: end" in fab_block[:100]

    # 4. CSS: Verify the form handles its own scroll.
    assert "#composer {" in css
    composer_block = css[css.index("#composer {"):]
    assert "overflow-y: auto" in composer_block[:150]


def test_required_stars_are_wrapped_in_spans(client: TestClient):
    """Verify that label text and required stars are wrapped in a <span> to keep them inline."""
    body = client.get("/").text
    
    # Check a few prominent ones
    assert '<span>Issue Title <span class="required-star">*</span></span>' in body
    assert '<span>Issue Number <span class="required-star">*</span></span>' in body
    
    # Check the article template
    article_tpl_start = body.index('id="article-template"')
    article_tpl_end = body.index("</template>", article_tpl_start)
    tpl = body[article_tpl_start:article_tpl_end]
    assert '<span>Headline <span class="required-star">*</span></span>' in tpl
    assert '<span>Article Link <span class="required-star">*</span></span>' in tpl


def test_toggle_preview_pinning(client: TestClient):
    """Verify that the preview header (and its toggle button) is pinned during scroll."""
    css = client.get("/static/composer.css").text
    
    assert ".preview-header" in css
    header_block = css[css.index(".preview-header"):]
    # Increase slice to ensure we catch rules later in the block
    assert "position: sticky" in header_block[:400]
    assert "top: 0" in header_block[:400]
    # Background must be opaque to hide scrolling text behind it
    assert "background:" in header_block[:400]

    # 2. CSS: Verify the content frame scrolls.
    assert ".preview-frame {" in css
    frame_block = css[css.index(".preview-frame {"):]
    assert "overflow: auto" in frame_block[:150]
