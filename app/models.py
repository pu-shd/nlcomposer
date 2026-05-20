"""Data models for a SmartDrivingCars newsletter issue.

The shape mirrors the patterns observed in the historical issue corpus
(`smartdrivingcar/_newsletters/<slug>/index.md`). Each section is optional
so partial drafts validate, but a fully-populated issue covers every block
seen in production newsletters.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Annotated, Literal, Optional

from pydantic import AnyHttpUrl, BaseModel, BeforeValidator, Field, model_validator

from .links import sanitize_url

# Any URL field on a NewsletterIssue runs through ``sanitize_url`` before
# pydantic does its own URL parsing — so a Microsoft Safelink pasted into
# the form (or arriving via the JSON API) gets unwrapped at the boundary
# and the rest of the app only ever sees the real target.
SanitizedHttpUrl = Annotated[AnyHttpUrl, BeforeValidator(sanitize_url)]


class Article(BaseModel):
    """One linked article in the body of the newsletter.

    Reflects the canonical pattern:
        ### [Headline](url)
        Byline, Date, "pull quote..." [Read more](url) Hmmmm... Alain comment
    """

    headline: str
    url: SanitizedHttpUrl
    byline: Optional[str] = None
    date_text: Optional[str] = Field(
        default=None,
        description='Free-form date as printed in the byline, e.g. "Apr 27"',
    )
    pull_quote: Optional[str] = None
    read_more_label: str = "Read more"
    read_more_url: Optional[SanitizedHttpUrl] = Field(
        default=None,
        description="Defaults to `url` when omitted.",
    )
    publication_logo_url: Optional[SanitizedHttpUrl] = Field(
        default=None,
        description="Small logo/icon shown next to the headline (favicon by default).",
    )
    publication_logo_media_id: Optional[int] = Field(
        default=None,
        description=(
            "Media Library id for an uploaded article logo. Preferred over "
            "``publication_logo_url`` when set — the in-browser preview "
            "renders it as ``/api/media/{id}/content``. (Mail-client "
            "inline embedding of per-article logos follows the planned "
            "``embed_all_media`` work; until then, uploaded article logos "
            "render in the preview but not in the recipient's inbox — "
            "use a public URL via the Logo URL field for now.)"
        ),
    )
    alain_comment: Optional[str] = Field(
        default=None,
        description='Optional "Hmmmm... Alain" commentary appended after Read more.',
    )

    def effective_read_more_url(self) -> AnyHttpUrl:
        return self.read_more_url or self.url


class PodcastTimecode(BaseModel):
    """One bullet in the podcast timecode list."""

    timecode: str = Field(description='As displayed, e.g. "0:50" or "17:35"')
    label: str
    url: SanitizedHttpUrl = Field(
        description="Deep link into the YouTube video at the timecode.",
    )


class PodcastEpisode(BaseModel):
    """A podcast/zoomcast episode block."""

    episode_number: Optional[int] = None
    title: str
    zoomcast_url: Optional[SanitizedHttpUrl] = None
    podcast_url: Optional[SanitizedHttpUrl] = None
    youtube_video_url: Optional[SanitizedHttpUrl] = None
    guest: Optional[str] = None
    summary: Optional[str] = None
    timecodes: list[PodcastTimecode] = Field(default_factory=list)


class YouTubeChannel(BaseModel):
    """A managed YouTube channel."""

    id: Optional[int] = None
    handle: str
    name: str
    channel_id: str
    uploads_playlist_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class YouTubeEpisode(BaseModel):
    """A video upload from a YouTube channel."""

    id: Optional[int] = None
    channel_id: int
    video_id: str
    title: str
    description: Optional[str] = None
    published_at: str
    thumbnail_url: Optional[str] = None
    payload: dict = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class StaticBlock(BaseModel):
    """A block item (sponsor blurb, book promo, etc.) displayed in the newsletter.

    Stored as raw HTML. Individual blocks can be collapsed and reordered
    within a Blocks section in the composer.
    """

    title: str
    html: str
    show_title: bool = True


class HighlightRef(BaseModel):
    """Reference to an article that should be repeated in the bottom highlights.

    Bottom highlights are rendered without icons or commentary, optionally
    grouped under the issue date the article was originally featured in.
    Two flavors of reference are supported:

    * **In-issue** — ``article_index`` points at one of the current
      issue's ``articles``. This is the original behavior and what every
      pre-archive-sourcing draft contains.
    * **Archive-sourced** — ``source_archive_id`` + ``source_article_index``
      reference an article from a previously-published issue stored in the
      ``archive`` table. The candidate-search endpoint resolves the
      reference once on pick and the JS round-trips the metadata via the
      ``cached_*`` fields, so the renderer never queries the archive at
      render time. The cached fields also let the template group
      highlights by source-issue date without any extra DB hop.

    Exactly one referencing kind must be set; the validator below enforces
    that. ``override_label`` continues to apply to either flavor.
    """

    # In-issue reference — historical shape; still the default for the
    # most common case where the editor recaps articles from THIS issue.
    article_index: Optional[int] = Field(
        default=None,
        description="Zero-based index into NewsletterIssue.articles.",
    )

    # Archive-sourced reference. Both ids must be set together; the JS
    # populates source_archive_id + source_article_index at pick time
    # alongside the cached_* fields below.
    source_archive_id: Optional[int] = Field(
        default=None,
        description="archive.id of the source issue (cross-issue highlight).",
    )
    source_article_index: Optional[int] = Field(
        default=None,
        description="Zero-based index into archive.payload.articles.",
    )

    # Renderable snapshot — populated by the SPA when an archive
    # candidate is picked, so the template needs no DB access to render
    # cross-issue highlights and groupings survive a draft save/load.
    cached_headline: Optional[str] = Field(
        default=None,
        description="Headline text for archive-sourced refs.",
    )
    cached_url: Optional[str] = Field(
        default=None,
        description="Article URL for archive-sourced refs.",
    )
    cached_issue_date: Optional[_date] = Field(
        default=None,
        description="Date of the source issue (used as the group heading).",
    )
    cached_issue_year: Optional[int] = Field(
        default=None,
        description="`issue_year` of the source issue.",
    )
    cached_issue_number: Optional[int] = Field(
        default=None,
        description="`issue_number` of the source issue.",
    )
    cached_issue_title: Optional[str] = Field(
        default=None,
        description="`issue_title` of the source issue (for display next to date).",
    )

    override_label: Optional[str] = Field(
        default=None,
        description="If set, replaces the headline text in the highlights list.",
    )

    @model_validator(mode="after")
    def _exactly_one_referencing_kind(self) -> "HighlightRef":
        """Reject refs that point nowhere — or point in two directions at once.

        Draft loading is the realistic culprit: an old draft that only
        sets ``article_index`` is still valid. A new draft that sets
        ``source_archive_id`` + ``source_article_index`` is valid. A row
        with neither (e.g. a half-filled form) is rejected so the server
        responds 422 with a clear pointer instead of rendering a blank
        list item; a row with both is ambiguous so it's rejected too.
        """
        has_in_issue = self.article_index is not None
        has_archive = (
            self.source_archive_id is not None
            and self.source_article_index is not None
        )
        partial_archive = (
            (self.source_archive_id is None) != (self.source_article_index is None)
        )
        if partial_archive:
            raise ValueError(
                "source_archive_id and source_article_index must be set together"
            )
        if has_in_issue and has_archive:
            raise ValueError(
                "highlight cannot reference both an in-issue article_index "
                "and a source_archive_id — pick one"
            )
        if not has_in_issue and not has_archive:
            raise ValueError(
                "highlight needs either article_index or source_archive_id+"
                "source_article_index"
            )
        return self

    def display_url(self, articles: "list[Article]") -> Optional[str]:
        """Return the URL to link the highlight to.

        Cross-issue refs lean on ``cached_url`` (captured at pick time);
        in-issue refs resolve through the current ``articles`` list so
        the URL stays in sync if the editor edits the article between
        adding the highlight and exporting the issue.
        """
        if self.article_index is not None and 0 <= self.article_index < len(articles):
            return str(articles[self.article_index].url)
        return self.cached_url

    def display_label(self, articles: "list[Article]") -> Optional[str]:
        """Return the visible link text, honoring ``override_label``."""
        if self.override_label:
            return self.override_label
        if self.article_index is not None and 0 <= self.article_index < len(articles):
            return articles[self.article_index].headline
        return self.cached_headline


class FormSection(BaseModel):
    """Render-plan entry describing the position of a sortable form
    section in the issue. The SPA emits one of these per visible
    Articles / Podcast / Static-blocks section, in DOM order, so the
    template can lay them out in the user's dragged order rather than
    the model-default "all articles, then podcast, then static blocks"
    sequence.

    For ``articles`` and ``static_blocks``, ``count`` is the number of
    entries the section contributes to the corresponding flat list on
    NewsletterIssue. The renderer walks that list with per-type
    cursors so the slice for each section is contiguous in the model
    and rendered back-to-back in the output.

    For ``podcast``, ``enabled`` mirrors the section's checkbox; only
    the first enabled podcast section consumes the singular
    ``NewsletterIssue.podcast`` slot.
    """

    type: Literal["articles", "podcast", "static_blocks"]
    id: Optional[str] = None
    count: Optional[int] = Field(
        default=None,
        ge=0,
        description="For articles / static_blocks: number of entries in this section.",
    )
    enabled: Optional[bool] = Field(
        default=None,
        description="For podcast: whether this section is the one that contributes the issue's podcast block.",
    )


class NewsletterIssue(BaseModel):
    """One full newsletter issue ready for rendering."""

    # Branding (mirrored from AppSettings.newsletter_name at build time
    # so renders are self-contained and round-trip safely without the
    # template needing access to the settings store).
    newsletter_name: str = "MyNewsletter"

    # Header / frontmatter
    issue_year: int = Field(description='e.g. 14 for "14th year"')
    issue_number: int = Field(description='e.g. 9 for "9th edition"')
    issue_title: str = Field(description="Short slug-like name for the edition")
    issue_date: _date
    display_date: Optional[str] = Field(
        default=None,
        description='Printed date string, e.g. "Sunday, May 3, 2026". Auto-generated if omitted.',
    )
    masthead_logo_url: Optional[SanitizedHttpUrl] = None
    masthead_logo_media_id: Optional[int] = Field(
        default=None,
        description=(
            "Media Library id for an uploaded masthead logo. Preferred over "
            "``masthead_logo_url`` when set — the eml builder resolves it to "
            "inline bytes (cid:) and the in-browser preview renders it as "
            "``/api/media/{id}/content``. ``url`` remains as a fallback if "
            "both are present and the media item is not yet ready."
        ),
    )
    masthead_logo_cid: Optional[str] = Field(
        default=None,
        description="Content-ID for an inline-attached logo image (set by the eml builder).",
    )
    intro_html: Optional[str] = Field(
        default=None,
        description="Optional intro paragraph(s) above the first article. Raw HTML.",
    )

    # Email envelope (defaults are sensible for the SmartDrivingCars list)
    from_name: str = "SmartDrivingCars"
    from_email: str = "newsletter@smartdrivingcar.com"
    to_email: str = "subscriber@example.com"
    reply_to: Optional[str] = None
    subject: Optional[str] = Field(
        default=None,
        description="Auto-built from issue_year/number/title when omitted.",
    )

    # Body sections — every one optional so partial drafts validate.
    articles: list[Article] = Field(default_factory=list)
    podcasts: list[PodcastEpisode] = Field(default_factory=list)
    static_blocks: list[StaticBlock] = Field(default_factory=list)
    highlights: list[HighlightRef] = Field(default_factory=list)
    # UI-driven render plan — describes which sortable section
    # (Articles / Podcast / Static blocks) appears at each position in
    # the user's dragged order. Empty means "legacy fixed order"
    # (all articles, then podcast, then all static blocks) so older
    # drafts continue to render exactly as they always did. See
    # rendered_sections() for the resolution logic.
    form_sections: list[FormSection] = Field(default_factory=list)

    footer_html: Optional[str] = None
    unsubscribe_url: Optional[SanitizedHttpUrl] = None

    # Render-time toggle propagated from AppSettings.include_read_more_link
    # so the same payload renders consistently whether served via /api/preview
    # or /api/eml. Default True preserves the historical behavior; the
    # composer client injects the configured value at build time.
    include_read_more_link: bool = True

    @model_validator(mode="before")
    @classmethod
    def migrate_podcast(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "podcast" in data and data["podcast"] and not data.get("podcasts"):
                data["podcasts"] = [data["podcast"]]
        return data

    def effective_subject(self) -> str:
        if self.subject:
            return self.subject
        return (
            f"{self.newsletter_name} {self.issue_year}.{self.issue_number:02d} — "
            f"{self.issue_title}"
        )

    def effective_display_date(self) -> str:
        if self.display_date:
            return self.display_date
        return self.issue_date.strftime("%A, %B %-d, %Y")

    def edition_line(self) -> str:
        suffix = _ordinal_suffix(self.issue_number)
        year_suffix = _ordinal_suffix(self.issue_year)
        return (
            f"{self.issue_number}{suffix} edition of the "
            f"{self.issue_year}{year_suffix} year of {self.newsletter_name} eLetter"
        )

    def header_slug(self) -> str:
        """The middle line of the masthead header — e.g. ``14.9-BeijingAutoShow-5.03.26``.

        Format intentionally differs from the .eml filename: issue number and
        month are unpadded, day and 2-digit year are zero-padded, mirroring
        the historical newsletter style.
        """
        d = self.issue_date
        return (
            f"{self.issue_year}.{self.issue_number}-{self.issue_title}-"
            f"{d.month}.{d.day:02d}.{d.year % 100:02d}"
        )

    def rendered_sections(self) -> list[dict]:
        """Resolve form_sections into a render plan the template walks.

        Returns a list of dicts, each carrying:

          - ``{"type": "articles", "articles": [Article, ...]}``
          - ``{"type": "podcast",  "podcast":  PodcastEpisode}``
          - ``{"type": "static_blocks", "blocks": [StaticBlock, ...]}``

        in the editor's dragged order. For ``articles`` and
        ``static_blocks``, the slice corresponds to the section's
        contiguous chunk of the flat list — per-type cursors advance
        through ``self.articles`` and ``self.static_blocks`` so
        multiple sections of the same type still get distinct slices.

        Empty / missing form_sections falls back to the legacy fixed
        order (articles → podcast → static_blocks) so pre-form_sections
        drafts and any direct API caller that doesn't bother emitting
        the metadata continues to render exactly as before.
        """
        if not self.form_sections:
            out: list[dict] = []
            if self.articles:
                out.append({"type": "articles", "articles": list(self.articles)})
            if self.podcasts:
                out.append({"type": "podcast", "podcasts": list(self.podcasts)})
            if self.static_blocks:
                out.append({"type": "static_blocks", "blocks": list(self.static_blocks)})
            if self.highlights:
                out.append({"type": "highlights", "groups": self.highlights_grouped()})
            return out

        art_cursor = 0
        sb_cursor = 0
        pod_cursor = 0
        hlt_cursor = 0
        out = []
        for fs in self.form_sections:
            if fs.type == "articles":
                n = fs.count or 0
                slice_ = list(self.articles[art_cursor : art_cursor + n])
                art_cursor += n
                if slice_:
                    out.append({"type": "articles", "articles": slice_})
            elif fs.type == "static_blocks":
                n = fs.count or 0
                slice_ = list(self.static_blocks[sb_cursor : sb_cursor + n])
                sb_cursor += n
                if slice_:
                    out.append({"type": "static_blocks", "blocks": slice_})
            elif fs.type == "podcast":
                n = fs.count or 0
                if n > 0:
                    slice_ = list(self.podcasts[pod_cursor : pod_cursor + n])
                    pod_cursor += n
                    if slice_:
                        out.append({"type": "podcast", "podcasts": slice_})
                elif getattr(fs, "enabled", False) and pod_cursor < len(self.podcasts):
                    # Legacy support for older drafts where podcast sections had enabled=True instead of count
                    out.append({"type": "podcast", "podcasts": [self.podcasts[pod_cursor]]})
                    pod_cursor += 1
            elif fs.type == "highlights":
                n = fs.count or 0
                if n > 0:
                    slice_ = list(self.highlights[hlt_cursor : hlt_cursor + n])
                    hlt_cursor += n
                    if slice_:
                        out.append({"type": "highlights", "groups": self.highlights_grouped(slice_)})

        # Defensive tail: if form_sections under-counts (a saved draft
        # whose counts drifted from the actual flat list), flush the
        # leftover entries in legacy order so nothing silently drops.
        leftover_articles = list(self.articles[art_cursor:])
        if leftover_articles:
            out.append({"type": "articles", "articles": leftover_articles})

        leftover_podcasts = list(self.podcasts[pod_cursor:])
        if leftover_podcasts:
            out.append({"type": "podcast", "podcasts": leftover_podcasts})

        leftover_blocks = list(self.static_blocks[sb_cursor:])
        if leftover_blocks:
            out.append({"type": "static_blocks", "blocks": leftover_blocks})

        leftover_highlights = list(self.highlights[hlt_cursor:])
        if leftover_highlights:
            out.append({"type": "highlights", "groups": self.highlights_grouped(leftover_highlights)})

        return out


    def highlights_grouped(self, subset: list[HighlightRef] | None = None) -> list[dict]:
        """Group bottom highlights by source issue, preserving drag order.

        Returns a list of ``{label, date, refs}`` dicts in the order the
        editor laid them out with the highlights drag handle. Groups
        appear in *first-occurrence* order — the first ref's group is
        the first group, the next previously-unseen ref's group is the
        second, and so on — so dragging an archive ref above all
        in-issue refs surfaces that archive group above the current
        issue. ``label`` is ``"This issue"`` for refs into the current
        articles or the source issue's formatted date + title for
        archive-sourced refs.

        Inside each group, refs preserve the user's drag order. URL-keyed
        dedup runs per-group (not globally) so a canonical article can
        intentionally appear under two different source dates if the
        editor wants to feature both editions.
        """
        groups: list[dict] = []
        index_by_key: dict = {}
        IN_ISSUE_KEY = "__in_issue__"

        for h in (subset if subset is not None else self.highlights):
            if h.article_index is not None:
                key = IN_ISSUE_KEY
                if key not in index_by_key:
                    index_by_key[key] = len(groups)
                    groups.append(
                        {
                            "label": "This issue",
                            "date": self.issue_date,
                            "refs": [],
                        }
                    )
                groups[index_by_key[key]]["refs"].append(h)
                continue
            d = h.cached_issue_date
            if d is None:
                # Defensive: archive-sourced ref missing the cached date
                # — bucket under a synthetic "Archive" group keyed by
                # title so the renderer doesn't drop the row silently.
                key = ("__archive_undated__", h.cached_issue_title or "")
                label = "Archive"
            else:
                key = (d.year, d.month, d.day, h.cached_issue_title or "")
                label = self._format_group_label(h)
            if key not in index_by_key:
                index_by_key[key] = len(groups)
                groups.append({"label": label, "date": d, "refs": []})
            groups[index_by_key[key]]["refs"].append(h)

        # Per-group URL dedup so a recap that appears twice in the same
        # source only renders once. For in-issue refs the URL comes from
        # display_url (override_label aware); for archive refs we use
        # cached_url. Either way: preserve the first occurrence so drag
        # order survives the dedup pass too.
        for group in groups:
            seen: set[str] = set()
            deduped: list[HighlightRef] = []
            for ref in group["refs"]:
                if ref.article_index is not None:
                    url = (ref.display_url(self.articles) or "").strip()
                else:
                    url = (ref.cached_url or "").strip()
                if url and url in seen:
                    continue
                if url:
                    seen.add(url)
                deduped.append(ref)
            group["refs"] = deduped
        return groups

    def _format_group_label(self, h: "HighlightRef") -> str:
        """Heading text for an archive-sourced group: date + edition."""
        d = h.cached_issue_date
        if d is None:
            return h.cached_issue_title or "Archive"
        # Match the human-friendly format used by ``effective_display_date``.
        base = d.strftime("%B %-d, %Y")
        if h.cached_issue_title:
            return f"{base} · {h.cached_issue_title}"
        return base


def _ordinal_suffix(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def compute_issue_year(issue_date: _date, initial_publication_date: _date) -> int:
    """Return the 1-based "Nth year" for ``issue_date``.

    The newsletter rolls over to a new "year of publication" each time the
    issue date crosses the anniversary of ``initial_publication_date``.
    Year 1 starts the day the newsletter launched, year 2 begins the day
    after the first anniversary, etc. Issues dated before the launch fall
    back to 1 (the field can never be 0 in the printed masthead).

    The composer UI computes this client-side from the configured
    ``AppSettings.initial_publication_date`` and the form's issue date,
    replacing the legacy "Year" input — so it lives here in the model
    layer where both client (mirrored in JS) and server-side tests can
    exercise the same arithmetic.
    """
    years = issue_date.year - initial_publication_date.year
    if (issue_date.month, issue_date.day) < (
        initial_publication_date.month,
        initial_publication_date.day,
    ):
        years -= 1
    return max(1, years + 1)
