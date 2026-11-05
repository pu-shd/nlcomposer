# Newsletter Composer

A single-page web app for composing email newsletters in a structured form
and exporting a ready-to-send `.eml` file that any mail client (Apple Mail,
Outlook, Thunderbird) can open, review, and send.

Goal: replace the manual "copy the headlines into Word, paste into
Thunderbird, swap the masthead image, fix the links" loop with a structured
form backed by automation — URL → article metadata, YouTube → podcast
timecodes, carry-forward of repeated blocks, and inline masthead embedding
so the recipient's inbox doesn't have to reach back to an external host
for the logo.

The shape is generic enough to drive any periodic newsletter that follows a
masthead + intro + articles + (optional) podcast + static blocks +
highlights + footer pattern. Default brand strings, "from" address, and
masthead logo are configured per-instance in the Settings dialog and
persist in the local SQLite database alongside drafts, templates,
archived issues, and the Logos & Media library.

---

## Table of contents

- [Features](#features)
- [Quick start (local, no Docker)](#quick-start-local-no-docker)
- [Quick start (Docker / Compose)](#quick-start-docker--compose)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [REST API reference](#rest-api-reference)
- [Data model](#data-model)
- [Customizing the newsletter brand](#customizing-the-newsletter-brand)
- [Persistence and data directory](#persistence-and-data-directory)
- [Deploying to Azure Container Apps](#deploying-to-azure-container-apps)
- [Development](#development)
- [License](#license)

---

## Features

- **Structured composer form** — a single page with a form pane and a live
  HTML preview pane. Sections (Articles, Podcast, Static blocks) can repeat
  and reorder via drag handles next to each disclosure triangle.
- **`.eml` export** — `Download` produces a multipart/alternative message
  with both HTML and plain-text bodies and (optionally) an inline-attached
  masthead logo referenced via `cid:`.
- **URL → article metadata** — per-article *Fetch* button scrapes
  `og:title`, byline, excerpt, and favicon and pre-fills empty fields.
- **YouTube → podcast metadata** — the Podcast section's *Fetch* button
  reads the oEmbed endpoint + page DOM for title, channel, and embedded
  timecodes.
- **Microsoft Safelinks unwrapping** — every URL field on a draft is run
  through a sanitizer so corporate-mail-rewrapped links don't leak the
  original recipient's tenant ID or break when the SafeLinks token expires.
- **Logos & Media library** — drop a PNG or JPG into the app's local
  library; per-article logos and static-block images can reference items
  by id (or by URL for hot-linked assets). Uploads are gated by
  MIME-sniff, magic-byte verification, size cap, and per-IP rate limit.
- **Drafts, templates, archive** — every save lives in SQLite. Built-in
  starter template seeds a sensible empty issue; archived issues store
  the rendered `.eml` bytes for replay.
- **Settings persistence** — newsletter name, from-address, unsubscribe
  URL, masthead logo (URL or upload), intro/footer HTML defaults, behavior
  toggles, and Logos & Media upload limits all live in a single row of
  the `settings` table.
- **Microsoft Entra ID gating (production)** — when deployed under Azure
  Container Apps' Easy Auth, access is restricted to the named principals
  configured at deploy time. The app itself is unaware of identity — it
  trusts the headers Easy Auth injects.

---

## Quick start (local, no Docker)

Requires Python ≥ 3.11.

```bash
make install      # creates .venv, installs runtime deps
make dev          # adds pytest + pytest-asyncio
make test         # runs the pytest suite (~340 tests)
make run          # uvicorn on http://127.0.0.1:8000 with --reload
```

Open <http://127.0.0.1:8000/> for the composer. Form on the left, live
preview on the right (debounced re-render on each keystroke), **Download**
in the topbar. Settings live behind the gear icon in the header.

Interactive API documentation at <http://127.0.0.1:8000/docs>; health
probe at `/healthz`.

Run a single test:

```bash
.venv/bin/pytest tests/test_eml.py::test_render_html_contains_required_blocks -q
```

---

## Quick start (Docker / Compose)

```bash
docker-compose up --build app                    # serve on :8000
docker-compose --profile test run --rm tests     # pytest inside the image
```

The image declares `VOLUME ["/app/data"]` and sets
`SDC_DATA_DIR=/app/data`, so production mounts (Azure Files, NFS, host
bind mount, named Docker volume) just bind there. The Compose definition
ships with a named `sdc-data` volume for local persistence.

---

## Configuration

The composer is configured at two layers.

### Per-instance environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `SDC_DATA_DIR` | `./data` | Directory holding `app.db`. Bind a volume here in production. |
| `SDC_SQLITE_VFS` | *(platform default)* | Set to `unix-dotfile` when `SDC_DATA_DIR` is a network share (Azure Files SMB, NFSv2/3). SQLite's default POSIX locks are unreliable on those filesystems. |
| `NLC_INSTANCE_JSON` / `NLC_INSTANCE_FILE` | *(unset: neutral engine)* | This deployment's brand config (JSON text, or a path to it; not both). See [Instance configuration](#instance-configuration). Invalid config stops startup. |

### Per-instance settings (UI-editable)

Stored as a single row in the SQLite `settings` table — JSON serialization
of `app.settings.AppSettings`. Configurable from **Settings** in the topbar:

- Newsletter brand: `newsletter_name`, `from_name`, `from_email`,
  `to_email`, `reply_to`, `unsubscribe_url`.
- Masthead: `masthead_logo_url` (hot-link) **or** `masthead_logo_media_id`
  (Logos & Media upload, inline-embedded as `cid:` in the exported `.eml`).
- Carry-forward copy: `default_intro_html`, `default_footer_html`.
- Issue numbering: `initial_publication_date` (anchor for the
  "Nth year" enumeration printed in the masthead).
- Behavior toggles: `preview_debounce_ms`, `auto_archive_on_download`,
  `include_read_more_link`, `confirm_before_delete`, `embed_all_media`.
- Logos & Media upload gating: `media_upload_max_bytes` (default 5 MiB),
  `media_upload_allowed_types` (default `image/png` + `image/jpeg`),
  `media_upload_rate_per_minute` (default 20), and
  `media_url_imports_enabled` (set false for fully air-gapped deployments).

Defaults live on the model; users without a `settings` row get them
automatically until they save once.

---

## Architecture

Thin MVC split, all glue in `app/main.py`:

- **Models** — `app/models.py` (`NewsletterIssue`, `Article`,
  `PodcastEpisode`, `StaticBlock`, `HighlightRef`), `app/settings.py`
  (`AppSettings`), and the `*_store.py` modules
  (`draft_store`, `template_store`, `archive_store`, `media_store`) that
  own SQLite CRUD.
- **Views** — Jinja templates in `app/templates/` (`newsletter.html.j2`,
  `newsletter.txt.j2`), the SPA in `app/static/`
  (`index.html`, `composer.js`, `composer.css`), and `app/eml.py` which
  renders HTML/text and assembles the multipart `.eml` via stdlib
  `email.message.EmailMessage`.
- **Controllers** — one `APIRouter` per resource under
  `app/controllers/` (`pages`, `preview`, `eml`, `settings`, `templates`,
  `drafts`, `archive`, `fetch`, `media`). `app/main.py` is a thin
  composition root that mounts them in a fixed order and runs `init_db`
  + `seed_default_templates` in the FastAPI lifespan.

Adding a new resource → add `app/<name>_store.py` (if persistent) +
`app/controllers/<name>.py` exposing a `router`, then register it in
`app/main.py` and `app/controllers/__init__.py`.

---

## REST API reference

The single-page UI is built on these endpoints. All accept and return
JSON unless noted.

### Pages

- `GET /` — composer SPA.
- `GET /healthz` — health probe; returns `{"status": "ok"}`.

### Preview

- `POST /api/preview/html` — rendered HTML body (text/html).
- `POST /api/preview/text` — rendered plain-text body (text/plain).

Body: a full `NewsletterIssue` payload.

### Export

- `POST /api/eml` — multipart `.eml` (`message/rfc822`) with a
  `Content-Disposition: attachment; filename="…"` header. When
  `auto_archive_on_download` is true, the same bytes are also written
  to the `archive` table in the same request. When the issue carries
  `masthead_logo_media_id` and the referenced item has cached bytes,
  the controller passes them as `masthead_logo_bytes` to `build_eml`
  so the resulting message embeds the logo via `cid:`.

### Settings

- `GET /api/settings` — current `AppSettings`, or model defaults if
  unset.
- `PUT /api/settings` — replace the stored settings. Validates that
  any `masthead_logo_media_id` points at a Logos & Media item with a
  non-empty `alt_text` (Image description) — 422 otherwise.

### Drafts, templates, archive

- `GET/POST /api/drafts`, `GET/PUT/DELETE /api/drafts/{id}`
- `GET/POST /api/templates`, `GET/PUT/DELETE /api/templates/{id}` — the
  built-in starter template (`builtin=1`) is protected against delete.
- `GET /api/archive`, `GET /api/archive/{id}` (JSON metadata),
  `GET /api/archive/{id}/eml` (raw bytes), `DELETE /api/archive/{id}`

### Logos & Media (formerly Media Library)

- `GET /api/media` — list (no bytes).
- `GET /api/media/{id}` — single item (no bytes).
- `POST /api/media` — register a URL-sourced item (`{kind, url}`);
  idempotent on URL.
- `POST /api/media/upload` — multipart upload of raw PNG/JPG bytes.
  Gated by `media_upload_max_bytes` (413 above),
  `media_upload_allowed_types` + magic-byte sniff (415 on mismatch),
  and per-IP `media_upload_rate_per_minute` (429 with `Retry-After`).
- `PUT /api/media/{id}` — set the Image description (`alt_text`).
  A non-empty description is required before the item can be referenced
  in any newsletter section.
- `DELETE /api/media/{id}` — remove a media item.
- `GET /api/media/{id}/content` — stream the cached bytes
  (`Content-Type` based on the persisted MIME).

### Fetch helpers

- `GET /api/fetch/article?url=…` — scrape `og:title`/`og:description`/
  meta author/favicon for a candidate article URL. Returns the same
  field names the Article form uses, so the JS can splat the response
  straight into empty inputs.
- `GET /api/fetch/podcast?url=…` — YouTube oEmbed + DOM read; returns
  title, channel, and (when present) parsed timecodes.

Interactive Swagger documentation at `/docs`.

---

## Data model

```text
NewsletterIssue
├── newsletter_name, issue_year, issue_number, issue_title, issue_date
├── from_name, from_email, to_email, reply_to
├── masthead_logo_url | masthead_logo_media_id          (mutually preferred)
├── intro_html
├── articles: [Article]
│     ├── headline, url, byline, date_text, pull_quote
│     ├── read_more_label, read_more_url
│     └── publication_logo_url | publication_logo_media_id
├── podcast: PodcastEpisode
│     ├── episode_number, title, guest, summary
│     ├── zoomcast_url, podcast_url, youtube_video_url
│     └── timecodes: [PodcastTimecode]
├── static_blocks: [StaticBlock { title, html }]
├── highlights: [HighlightRef { article_index, override_label? }]
├── footer_html
└── unsubscribe_url
```

Every URL field is wrapped as `SanitizedHttpUrl` — an `AnyHttpUrl` with
a `BeforeValidator(sanitize_url)` — so Microsoft Safelinks and similar
tracking wrappers are unwrapped at the model boundary. Downstream code
(preview, eml, fetch) only ever sees the real target.

---

## Customizing the newsletter brand

The renderer is a Jinja template (`app/templates/newsletter.html.j2`)
that reads strings off the `NewsletterIssue` and `AppSettings` payloads,
not from constants. To rebrand a deployment:

1. **Settings → Newsletter brand**: change `newsletter_name`, `from_*`,
   `to_email`, `unsubscribe_url`.
2. **Settings → Masthead logo**: paste a public URL into "Masthead logo
   URL", *or* click **Upload** to push a PNG/JPG into Logos & Media.
   Open Logos & Media and set the new item's Image description before
   pressing Save — Settings rejects un-described uploads (parity with
   the same rule on article logos and static-block images).
3. **Settings → Default intro / footer HTML**: HTML that lands above
   the first article and at the bottom of every new draft.
4. **Settings → Issue numbering**: `initial_publication_date` is the
   anchor used to compute the "Nth year" enumeration printed in the
   masthead. Set it to the date your newsletter first shipped.

Defaults that should hold for every editor of a deployment (brand name,
inception date, footer, the starter template) belong in that deployment's
`instance.json` — see [Instance configuration](#instance-configuration).

The default template seeded into the `templates` table on first run is
intentionally empty — it's the starting point a new editor sees when
they click **Compose New**. Save your own as a Template once you have
a good baseline.

---

## Persistence and data directory

A single SQLite database at `${SDC_DATA_DIR:-./data}/app.db` backs
every persistent surface:

- `settings`  — single-row JSON blob of `AppSettings`.
- `drafts`    — saved-but-incomplete issues being edited.
- `templates` — preset starting points for new issues.
- `archive`   — finalized issues with their rendered `.eml` bytes.
- `media`     — Logos & Media: cached PNG/JPG bytes plus alt text used
  by article logos and static-block images.

Each issue/payload is stored as JSON text so the schema stays stable as
`NewsletterIssue` evolves; the application is responsible for the JSON
shape on read.

`init_db` is idempotent — it runs on every FastAPI lifespan start with
`CREATE TABLE IF NOT EXISTS` for every table, so a freshly-mounted
empty volume self-bootstraps on the first request.

---

## Deploying to Azure Container Apps

This repository is the **engine**: public, unit-agnostic, and holding no
deployment credentials. Each department, unit or program that runs a
newsletter keeps a small **private config repo** that pins an engine
release and deploys it through GitOps. The template is
[`examples/config-repo/`](examples/config-repo/); copy it to start one.

```
pu-<unit>/nlcomposer-config (private)          pu-shd/nlcomposer (public, this repo)
├── template.lock ── pins tag + commit sha ──▶ release vX.Y.Z (template.lock asset)
├── <unit>/deploy.env      Azure targets (not secret)
├── <unit>/instance.json   brand: footer, settings defaults, starter template
└── .github/workflows/
    ├── deploy.yml         merge to main → tools/lock.sh → checkout engine@sha
    │                      → scripts/ci-deploy.sh (build in the unit's ACR,
    │                        roll revision, wait healthy, smoke test)
    ├── validate.yml       required check: lock + config vs pinned engine
    └── update-check.yml   daily: newer release → PR changing only template.lock
```

**Shipping a change.** Merge to `main` here (tests are the required
check), then tag `vX.Y.Z` on the merged commit. `release.yml` re-runs the
tests, checks the commit is on `main`, and publishes a release with
`template.lock`. Each config repo's update check opens a PR bumping its
lock; **merging that PR is the deploy**. Nothing is pushed to a registry:
each deployment builds the image in its own Azure Container Registry from
the pinned commit (`<app>:<tag>-<sha7>`, reused if present).

**CI identity.** A user-assigned managed identity per deployment, with
Contributor on that deployment's resource group only, federated (OIDC) to
`repo:<config repo>:environment:<unit>`. The config repo stores its client,
tenant and subscription IDs as repository variables; there are no secrets.
App secrets (for example `youtube-api-key`) live on the Container App and
are referenced, never passed through GitHub.

### Instance configuration

Anything brand-specific comes from `<unit>/instance.json`, passed to the
container as `NLC_INSTANCE_JSON` (or `NLC_INSTANCE_FILE` for local runs).
It is validated strictly at startup — a bad file stops the container, and
the previous revision keeps serving — and in CI via
`python -m app.instance check <file>`.

```json
{
  "footer_fallback_html": "<p>This newsletter is maintained by …</p>",
  "settings_defaults": {"newsletter_name": "…", "initial_publication_date": "2013-01-01"},
  "default_template": {"name": "… Default", "description": "…", "payload": {"from_name": "…"}}
}
```

| Key | Effect |
|---|---|
| `footer_fallback_html` | Footer rendered when an issue has no `footer_html` (HTML and plain-text renderings). |
| `settings_defaults` | Starting values for any `AppSettings` field. Precedence: engine default < instance default < value saved in Settings, so these shape a fresh install and fill keys an older saved row predates, but never overwrite a user's edit. |
| `default_template` | Name, description and payload (merged over the neutral starter) of the built-in template. Seeding matches by name, so keep the name stable once deployed. |

### Operator CLI

`scripts/deploy.sh` provisions a new deployment end to end — resource
group, storage + file share, ACR, Container Apps environment and app,
Entra ID (Easy Auth), and the CI identity wiring above. macOS zsh;
requires `az` and `gh`.

```bash
scripts/deploy.sh setup     --config ../nlcomposer-config/<unit>   # interactive, idempotent
scripts/deploy.sh domain    --config ../nlcomposer-config/<unit>   # custom hostname + managed cert
scripts/deploy.sh status    --config ../nlcomposer-config/<unit>
scripts/deploy.sh update    --config ../nlcomposer-config/<unit>   # roll this working tree (dev-<sha7>)
scripts/deploy.sh wipe-data --config ../nlcomposer-config/<unit>   # DELETE app.db (guarded)
scripts/deploy.sh teardown  --config ../nlcomposer-config/<unit>
```

`update` runs the same `scripts/ci-deploy.sh` as CI, from a clean working
tree; normal deploys go through the config repo. Every command accepts
`--dry-run`.

---

## Development

Layout:

```text
app/
  main.py              # composition root — routers + lifespan, no logic
  models.py            # NewsletterIssue + Article + Podcast + …
  settings.py          # AppSettings + load/save
  db.py                # connect / init_db / get_db / session
  eml.py               # render_html, render_text, build_eml
  links.py             # Microsoft Safelinks unwrap
  fetch.py             # article metadata scraper
  podcast.py           # YouTube oEmbed + timecode parsing
  media_store.py       # Logos & Media: SQLite CRUD + MIME sniffing
  rate_limit.py        # per-IP token bucket for uploads
  archive_store.py     # finalized-issue store
  draft_store.py       # work-in-progress store
  template_store.py    # template store + builtin seeder
  controllers/         # one APIRouter per resource
  templates/           # Jinja: newsletter.html.j2 / newsletter.txt.j2
  static/              # SPA: index.html, composer.js, composer.css
app/instance.py        # per-deployment instance config (NLC_INSTANCE_JSON / _FILE)
tests/                 # pytest suite
scripts/deploy.sh      # operator CLI: provision / domain / teardown a deployment
scripts/ci-deploy.sh   # the rollout a config repo's CI runs
examples/config-repo/  # template for a unit's private config repo
.github/workflows/     # ci.yml (tests + actionlint), release.yml (template.lock)
```

Coding conventions:

- `app/main.py` is intentionally thin — no route logic. Put handlers in
  a controller.
- Stores expose plain functions (`create_*`, `get_*`, `list_*`,
  `update_*`, `delete_*`) plus typed Pydantic models for `Create` /
  `Update` / `Summary` / full shapes. Controllers translate store
  exceptions (e.g. `TemplateNameConflictError`, `BuiltinDeleteError`)
  into `HTTPException`.
- The `.eml` filename format (`{year}.{number:02d}-{slug}-{iso-date}.eml`)
  and the masthead `header_slug` format are tied to the historical
  archive style — change them only deliberately.
- Every URL field on a `NewsletterIssue` (or `AppSettings`) is
  `SanitizedHttpUrl`. Keep this when adding URL fields so Safelinks
  and similar wrappers get unwrapped at the model boundary.

### Tests

Pytest config lives in `pyproject.toml` (`testpaths=["tests"]`,
`asyncio_mode = "auto"`). `tests/conftest.py` has an autouse fixture
that points `SDC_DATA_DIR` at `tmp_path` for every test, so the
FastAPI lifespan doesn't touch the developer's working `data/`
directory. There's also a `sample_issue` fixture; prefer it over
hand-building a `NewsletterIssue` in new tests.

`test_ui.py` covers single-page composer behavior and is the slowest
file; `test_eml.py`, `test_fetch.py`, and `test_podcast.py` exercise
the renderers and scrapers.

### Adding dependencies

Runtime deps live in `requirements.txt` (canonical for Docker / Azure)
and in `pyproject.toml`'s `dependencies` (canonical for `pip install
-e .`). Keep them in sync.

---

## License

MIT. See [LICENSE.md](LICENSE.md). Copyright © 2026 Princeton University.
