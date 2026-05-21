"""Render a NewsletterIssue into a multipart/alternative .eml message.

We use the stdlib ``email.message.EmailMessage`` so MIME boundaries, encoding,
and Content-IDs are produced correctly. The HTML body is rendered from a
Jinja2 template; the plain-text body is rendered from a sibling template so
non-HTML clients still get a readable message.

A masthead logo can be attached inline (as ``image/png`` or ``image/jpeg``)
and referenced from the HTML via ``cid:masthead-logo``.
"""

from __future__ import annotations

import mimetypes
import re
import sqlite3
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .media_store import get_media, get_media_bytes
from .models import NewsletterIssue
from .settings import load_settings

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=False,
    lstrip_blocks=False,
)


def render_html(issue: NewsletterIssue) -> str:
    return _env.get_template("newsletter.html.j2").render(issue=issue)


def render_text(issue: NewsletterIssue) -> str:
    return _env.get_template("newsletter.txt.j2").render(issue=issue)


def build_eml(
    issue: NewsletterIssue,
    *,
    conn: Optional[sqlite3.Connection] = None,
    masthead_logo_bytes: Optional[bytes] = None,
    masthead_logo_filename: str = "masthead.png",
) -> bytes:
    """Build a complete RFC-5322 .eml message as bytes.

    If ``masthead_logo_bytes`` is provided, the issue's ``masthead_logo_cid``
    is set on a copy of the issue so the HTML template references the inline
    attachment. The original issue object is not mutated.
    """

    if masthead_logo_bytes is not None:
        cid = make_msgid(domain="newsletter-composer.local")
        # Strip surrounding angle brackets for use inside ``cid:`` URI.
        bare_cid = cid.strip("<>")
        issue = issue.model_copy(update={"masthead_logo_cid": bare_cid})

    msg = EmailMessage()
    msg["Subject"] = issue.effective_subject()
    msg["From"] = f"{issue.from_name} <{issue.from_email}>"
    msg["To"] = issue.to_email
    if issue.reply_to:
        msg["Reply-To"] = issue.reply_to
    msg["Date"] = formatdate(localtime=True)
    msg["MIME-Version"] = "1.0"

    text_body = render_text(issue)
    html_body = render_html(issue)

    images_to_attach = []

    if conn is not None:
        try:
            settings = load_settings(conn)
            embed_enabled = settings.embed_all_media
        except Exception as e:
            print(f"Warning: Failed to load settings from DB: {e}")
            embed_enabled = True

        soup = BeautifulSoup(html_body, "html.parser")
        modified = False

        for img in soup.find_all("img"):
            src = img.get("src")
            if not src:
                continue

            match = re.match(r"^/api/media/(\d+)/content", src)
            if match:
                media_id = int(match.group(1))
                if embed_enabled:
                    bytes_and_type = get_media_bytes(conn, media_id)
                    if bytes_and_type is not None:
                        img_bytes, content_type = bytes_and_type
                        media_item = get_media(conn, media_id)
                        filename = (media_item.filename if media_item else None) or f"media-{media_id}.png"

                        cid = make_msgid(domain="newsletter-composer.local")
                        bare_cid = cid.strip("<>")

                        img["src"] = f"cid:{bare_cid}"
                        modified = True

                        images_to_attach.append({
                            "bytes": img_bytes,
                            "content_type": content_type,
                            "cid": cid,
                            "filename": filename,
                        })
                    else:
                        media_item = get_media(conn, media_id)
                        if media_item is not None and media_item.url:
                            img["src"] = str(media_item.url)
                            modified = True
                            print(f"Warning: Media bytes not found for ID {media_id}. Falling back to URL: {media_item.url}")
                        else:
                            print(f"Warning: Media bytes not found for ID {media_id} and no fallback URL exists.")
                else:
                    media_item = get_media(conn, media_id)
                    if media_item is not None and media_item.url:
                        img["src"] = str(media_item.url)
                        modified = True

        if modified:
            html_body = str(soup)

    msg.set_content(text_body, subtype="plain", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    html_part = msg.get_payload()[1]

    if masthead_logo_bytes is not None:
        ctype, _ = mimetypes.guess_type(masthead_logo_filename)
        maintype, subtype = (ctype or "image/png").split("/", 1)
        # The HTML alternative is the second part of the alternative payload;
        # we need to attach the inline image inside that part as related.
        # Re-wrap the HTML part as multipart/related so the cid resolves.
        # EmailMessage supports add_related on a singlepart.
        html_part.add_related(
            masthead_logo_bytes,
            maintype=maintype,
            subtype=subtype,
            cid=f"<{issue.masthead_logo_cid}>",
            filename=masthead_logo_filename,
        )

    for attachment in images_to_attach:
        ctype = attachment["content_type"] or "image/png"
        maintype, subtype = ctype.split("/", 1)
        html_part.add_related(
            attachment["bytes"],
            maintype=maintype,
            subtype=subtype,
            cid=attachment["cid"],
            filename=attachment["filename"],
        )

    return bytes(msg)


def build_eml_filename(issue: NewsletterIssue) -> str:
    """Return a filesystem-safe filename like ``14.09-BeijingAutoShow-2026-05-03.eml``."""
    safe_title = "".join(
        c if c.isalnum() or c in "-_" else "-" for c in issue.issue_title
    ).strip("-")
    return (
        f"{issue.issue_year}.{issue.issue_number:02d}-{safe_title}-"
        f"{issue.issue_date.isoformat()}.eml"
    )
