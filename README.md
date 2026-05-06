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

Then POST a `NewsletterIssue` JSON to:

- `POST /api/preview/html` — rendered HTML body
- `POST /api/preview/text` — rendered plain-text body
- `POST /api/eml` — full multipart `.eml` (downloads as attachment)

Swagger UI is available at `/docs`.

## Quick start (Docker)

```bash
docker-compose up --build app          # serve on :8000
docker-compose --profile test run --rm tests
```

## Status

This is the bootstrap iteration. The composer UI, URL/YouTube automation,
draft persistence, and SMTP send are tracked as follow-up tasks.
