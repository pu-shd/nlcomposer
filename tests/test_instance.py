"""Per-deployment instance configuration (``app.instance``).

The engine is public and unit-agnostic; anything brand-specific (the footer
shown when an issue has none, AppSettings starting values, the built-in
starter template) comes from the deployment's config repo via
``NLC_INSTANCE_JSON`` / ``NLC_INSTANCE_FILE``. These tests pin:

* neutral behavior when no instance config is supplied;
* fail-fast validation (bad JSON, unknown keys, invalid values, ambiguity);
* precedence: engine defaults < instance defaults < values saved in the UI;
* the footer fallback in both HTML and text renderings;
* that the seeded starter template follows the instance's name, so an
  existing deployment's template is matched instead of duplicated.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import instance
from app.db import connect, init_db
from app.eml import render_html, render_text
from app.models import NewsletterIssue
from app.settings import AppSettings, load_settings, save_settings
from app.template_store import get_template, list_templates, seed_default_templates

REPO_ROOT = Path(__file__).resolve().parent.parent

SDC_LIKE = {
    "footer_fallback_html": "<p>This newsletter is maintained by Prof. Example, Example University.</p>",
    "settings_defaults": {
        "newsletter_name": "Example Letter",
        "initial_publication_date": "2013-01-01",
    },
    "default_template": {
        "name": "Example Letter Default",
        "description": "Starter for Example Letter.",
        "payload": {"issue_year": 14, "from_name": "Prof. Example"},
    },
}


def _use(monkeypatch, cfg: dict | str | None = None, *, file: Path | None = None) -> None:
    monkeypatch.delenv("NLC_INSTANCE_JSON", raising=False)
    monkeypatch.delenv("NLC_INSTANCE_FILE", raising=False)
    if cfg is not None:
        monkeypatch.setenv("NLC_INSTANCE_JSON", cfg if isinstance(cfg, str) else json.dumps(cfg))
    if file is not None:
        monkeypatch.setenv("NLC_INSTANCE_FILE", str(file))
    instance.reset_instance_config_cache()


def _issue(**kw) -> NewsletterIssue:
    base = dict(issue_year=1, issue_number=1, issue_title="t", issue_date=date(2026, 1, 5))
    base.update(kw)
    return NewsletterIssue(**base)


# ── Loading ──────────────────────────────────────────────────────────────

def test_no_instance_config_is_neutral(monkeypatch):
    _use(monkeypatch)
    cfg = instance.get_instance_config()
    assert cfg.footer_fallback_html is None
    assert cfg.settings_defaults == {}
    assert cfg.default_template.name == "Default"


def test_json_env_is_applied(monkeypatch):
    _use(monkeypatch, SDC_LIKE)
    cfg = instance.get_instance_config()
    assert cfg.default_template.name == "Example Letter Default"
    assert "Prof. Example" in cfg.footer_fallback_html


def test_file_env_is_applied(monkeypatch, tmp_path):
    f = tmp_path / "instance.json"
    f.write_text(json.dumps(SDC_LIKE))
    _use(monkeypatch, file=f)
    assert instance.get_instance_config().default_template.name == "Example Letter Default"


def test_both_sources_set_is_ambiguous(monkeypatch, tmp_path):
    f = tmp_path / "instance.json"
    f.write_text("{}")
    _use(monkeypatch, {}, file=f)
    with pytest.raises(instance.InstanceConfigError, match="both"):
        instance.get_instance_config()


def test_missing_file_fails(monkeypatch, tmp_path):
    _use(monkeypatch, file=tmp_path / "nope.json")
    with pytest.raises(instance.InstanceConfigError):
        instance.get_instance_config()


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ("{not json", "JSON"),
        ("[]", "object"),
        ({"surprise": 1}, "surprise"),
        ({"settings_defaults": {"no_such_setting": 1}}, "no_such_setting"),
        ({"settings_defaults": {"preview_debounce_ms": -5}}, "preview_debounce_ms"),
        ({"default_template": {"payload": {"no_such_field": 1}}}, "no_such_field"),
        ({"default_template": {"name": ""}}, "name"),
    ],
)
def test_invalid_config_fails_fast_with_a_useful_message(monkeypatch, bad, fragment):
    _use(monkeypatch, bad)
    with pytest.raises(instance.InstanceConfigError, match=re.escape(fragment)):
        instance.get_instance_config()


def test_app_refuses_to_start_with_invalid_config(monkeypatch):
    _use(monkeypatch, {"surprise": 1})
    from app.main import app

    with pytest.raises(instance.InstanceConfigError):
        with TestClient(app):
            pass


# ── Settings precedence ──────────────────────────────────────────────────

def test_engine_defaults_are_neutral():
    s = AppSettings()
    assert s.initial_publication_date is None
    issue = _issue()
    for value in (issue.from_name, issue.from_email):
        assert not re.search(r"(?i)smart ?driving", value)


def test_instance_defaults_apply_when_nothing_saved(monkeypatch):
    _use(monkeypatch, SDC_LIKE)
    conn = connect()
    init_db(conn)
    s = load_settings(conn)
    assert s.newsletter_name == "Example Letter"
    assert s.initial_publication_date == date(2013, 1, 1)


def test_saved_settings_win_over_instance_defaults(monkeypatch):
    _use(monkeypatch, SDC_LIKE)
    conn = connect()
    init_db(conn)
    save_settings(conn, AppSettings(newsletter_name="Edited In UI"))
    conn.commit()
    assert load_settings(conn).newsletter_name == "Edited In UI"


def test_instance_default_fills_a_key_absent_from_an_older_saved_row(monkeypatch):
    """A row saved before a setting existed lacks that key; the instance default applies."""
    _use(monkeypatch, {"settings_defaults": {"podcast_title": "Example Podcast"}})
    conn = connect()
    init_db(conn)
    conn.execute(
        "INSERT INTO settings (id, payload, updated_at) VALUES (1, ?, 'x')",
        (json.dumps({"newsletter_name": "Saved"}),),
    )
    conn.commit()
    s = load_settings(conn)
    assert s.newsletter_name == "Saved"
    assert s.podcast_title == "Example Podcast"


# ── Footer fallback ──────────────────────────────────────────────────────

def test_no_fallback_footer_renders_no_maintainer_line(monkeypatch):
    _use(monkeypatch)
    assert "maintained by" not in render_html(_issue())
    assert "maintained by" not in render_text(_issue())


def test_fallback_footer_renders_when_issue_has_none(monkeypatch):
    _use(monkeypatch, SDC_LIKE)
    html = render_html(_issue())
    text = render_text(_issue())
    assert "maintained by Prof. Example, Example University." in html
    assert "maintained by Prof. Example, Example University." in text
    assert "<p>" not in text.split("maintained by")[0][-10:]


def test_issue_footer_overrides_fallback(monkeypatch):
    _use(monkeypatch, SDC_LIKE)
    html = render_html(_issue(footer_html="<p>Issue-specific footer</p>"))
    assert "Issue-specific footer" in html
    assert "Prof. Example" not in html


# ── Starter template ─────────────────────────────────────────────────────

def test_seed_uses_neutral_template_by_default(monkeypatch):
    _use(monkeypatch)
    conn = connect()
    init_db(conn)
    seed_default_templates(conn)
    builtins = [t for t in list_templates(conn) if t.builtin]
    assert [t.name for t in builtins] == ["Default"]
    assert get_template(conn, builtins[0].id).payload.get("static_blocks", []) == []


def test_seed_uses_instance_template_merged_over_neutral(monkeypatch):
    _use(monkeypatch, SDC_LIKE)
    conn = connect()
    init_db(conn)
    seed_default_templates(conn)
    (summary,) = [t for t in list_templates(conn) if t.builtin]
    t = get_template(conn, summary.id)
    assert t.name == "Example Letter Default"
    assert t.payload["from_name"] == "Prof. Example"
    assert t.payload["issue_year"] == 14
    assert "articles" in t.payload  # neutral keys still present


def test_seed_matches_existing_template_by_instance_name(monkeypatch):
    _use(monkeypatch, SDC_LIKE)
    conn = connect()
    init_db(conn)
    seed_default_templates(conn)
    seed_default_templates(conn)
    assert [t.name for t in list_templates(conn) if t.builtin] == ["Example Letter Default"]


# ── CLI used by config repos ─────────────────────────────────────────────

def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "app.instance", *args],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )


def test_cli_check_accepts_valid_file(tmp_path):
    f = tmp_path / "instance.json"
    f.write_text(json.dumps(SDC_LIKE))
    r = _cli("check", str(f))
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout.lower()


def test_cli_check_rejects_invalid_file(tmp_path):
    f = tmp_path / "instance.json"
    f.write_text(json.dumps({"surprise": 1}))
    r = _cli("check", str(f))
    assert r.returncode == 1
    assert "surprise" in r.stderr


def test_cli_without_arguments_is_a_usage_error():
    r = _cli()
    assert r.returncode == 2


# ── The public engine carries no deployment's brand ─────────────────────

BRAND = re.compile(r"(?i)smart ?driving|smartdrivingcar")


def test_engine_source_is_brand_free():
    offenders = []
    for path in (REPO_ROOT / "app").rglob("*"):
        if path.suffix in {".py", ".j2", ".html", ".js", ".css"} and path.is_file():
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if BRAND.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()[:80]}")
    assert offenders == []
