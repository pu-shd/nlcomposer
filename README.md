# sdc-composer

Web app for composing the **SmartDrivingCars** weekly newsletter and producing
a `.eml` file that mirrors the layout of the historical issues. Goal: replace
the manual Thunderbird/Outlook copy-and-paste loop with a structured form
backed by automation (URL → article metadata, YouTube → podcast timecodes,
carry-forward of repeated blocks).

The data model captures the recurring patterns observed in the historical
corpus (`smartdrivingcar/_newsletters/`):

- Masthead with logo and edition line ("9th edition of the 14th year…")
- Article entries: headline link, byline, pull quote, "Read more", optional
  publication logo, optional "Hmmmm… Alain" comment
- Podcast block with ZoomCast/Podcasts links and timecode bullets
- Static carry-forward blocks (sponsor, book promo, conferences)
- Bottom "highlights" repeat (no icons / no commentary)
- Footer with unsubscribe link

## Quick start (local, no Docker)

```bash
make install      # creates .venv, installs runtime deps
make dev          # installs pytest etc.
make test         # runs the pytest suite
make run          # uvicorn on http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000/` for the single-page composer (form on the
left, live preview on the right, **Download .eml** in the topbar). Each
article row also has a **Fetch** button that scrapes the URL for
headline / byline / excerpt / favicon and pre-fills empty fields.

API endpoints used by the UI:

- `POST /api/preview/html` — rendered HTML body
- `POST /api/preview/text` — rendered plain-text body
- `POST /api/eml` — full multipart `.eml` (downloads as attachment)
- `GET  /api/fetch/article?url=…` — scrape og:title / og:description /
  meta author / favicon for a candidate article URL

Swagger UI is available at `/docs`.

## Quick start (Docker)

```bash
docker-compose up --build app          # serve on :8000
docker-compose --profile test run --rm tests
```

## Status

Composer UI and article-URL automation are live. YouTube/podcast timecode
parsing, draft persistence, and SMTP send are tracked as follow-up tasks.
