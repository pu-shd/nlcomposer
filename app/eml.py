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
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import NewsletterIssue

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
    masthead_logo_bytes: Optional[bytes] = None,
    masthead_logo_filename: str = "masthead.png",
) -> bytes:
    """Build a complete RFC-5322 .eml message as bytes.

    If ``masthead_logo_bytes`` is provided, the issue's ``masthead_logo_cid``
    is set on a copy of the issue so the HTML template references the inline
    attachment. The original issue object is not mutated.
    """

    if masthead_logo_bytes is not None:
        cid = make_msgid(domain="sdc-composer.local")
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
    msg.set_content(text_body, subtype="plain", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    if masthead_logo_bytes is not None:
        ctype, _ = mimetypes.guess_type(masthead_logo_filename)
        maintype, subtype = (ctype or "image/png").split("/", 1)
        # The HTML alternative is the second part of the alternative payload;
        # we need to attach the inline image inside that part as related.
        html_part = msg.get_payload()[1]
        # Re-wrap the HTML part as multipart/related so the cid resolves.
        # EmailMessage supports add_related on a singlepart.
        html_part.add_related(
            masthead_logo_bytes,
            maintype=maintype,
            subtype=subtype,
            cid=f"<{issue.masthead_logo_cid}>",
            filename=masthead_logo_filename,
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
