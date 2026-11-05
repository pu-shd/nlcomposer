"""Per-deployment instance configuration.

The engine is public and unit-agnostic. Everything that belongs to one
newsletter — the footer printed when an issue has none, starting values for
:class:`~app.settings.AppSettings`, and the built-in starter template — is
supplied by the deployment (its config repo), never hard-coded here.

Source, exactly one of (setting both is an error):

* ``NLC_INSTANCE_JSON`` — the JSON document itself. Container Apps
  deployments pass it this way.
* ``NLC_INSTANCE_FILE`` — path to a JSON file (local runs, docker-compose).

Neither set means an empty config: neutral engine defaults.

Precedence for settings: engine defaults < ``settings_defaults`` < values
saved from the Settings dialog. Instance defaults therefore shape a fresh
install and fill keys an older saved row predates; they never overwrite what
a user saved.

Trust: the config is operator-controlled (reviewed in the config repo), so
``footer_fallback_html`` is rendered as-is, like the other operator-supplied
HTML defaults.

Validation is strict and runs at startup (``app.main`` lifespan), so a bad
config stops the container instead of serving a half-configured newsletter.
Config repos run the same check in CI::

    python -m app.instance check path/to/instance.json
"""

from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ENV_JSON = "NLC_INSTANCE_JSON"
ENV_FILE = "NLC_INSTANCE_FILE"


class InstanceConfigError(RuntimeError):
    """The deployment's instance configuration is missing, malformed or invalid."""


class DefaultTemplateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="Default", min_length=1, max_length=120)
    description: str = "Empty starter issue."
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Partial NewsletterIssue merged over the neutral starter.",
    )

    @model_validator(mode="after")
    def _payload_fields_exist(self) -> "DefaultTemplateConfig":
        from .models import NewsletterIssue

        unknown = sorted(set(self.payload) - set(NewsletterIssue.model_fields))
        if unknown:
            raise ValueError(f"default_template.payload has unknown fields: {', '.join(unknown)}")
        return self


class InstanceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    footer_fallback_html: Optional[str] = Field(
        default=None,
        description="Footer rendered when an issue has no footer_html of its own.",
    )
    settings_defaults: dict[str, Any] = Field(default_factory=dict)
    default_template: DefaultTemplateConfig = Field(default_factory=DefaultTemplateConfig)

    @model_validator(mode="after")
    def _settings_defaults_valid(self) -> "InstanceConfig":
        from .settings import AppSettings

        unknown = sorted(set(self.settings_defaults) - set(AppSettings.model_fields))
        if unknown:
            raise ValueError(f"settings_defaults has unknown settings: {', '.join(unknown)}")
        try:
            AppSettings.model_validate(self.settings_defaults)
        except ValidationError as exc:
            fields = ", ".join(str(e["loc"][0]) for e in exc.errors() if e.get("loc"))
            raise ValueError(f"settings_defaults has invalid values for: {fields}") from exc
        return self


def parse_instance_config(text: str, *, source: str = "instance config") -> InstanceConfig:
    """Parse and validate one instance document; raise InstanceConfigError on any problem."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstanceConfigError(f"{source}: not valid JSON ({exc.msg}, line {exc.lineno})") from exc
    if not isinstance(data, dict):
        raise InstanceConfigError(f"{source}: must be a JSON object, got {type(data).__name__}")
    try:
        return InstanceConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
        )
        raise InstanceConfigError(f"{source}: {problems}") from exc


def load_instance_config(env: Optional[dict[str, str]] = None) -> InstanceConfig:
    env = os.environ if env is None else env
    raw = env.get(ENV_JSON)
    path = env.get(ENV_FILE)
    if raw and path:
        raise InstanceConfigError(f"set {ENV_JSON} or {ENV_FILE}, not both")
    if raw:
        return parse_instance_config(raw, source=ENV_JSON)
    if path:
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise InstanceConfigError(f"{ENV_FILE}={path}: cannot read ({exc.strerror})") from exc
        return parse_instance_config(text, source=str(p))
    return InstanceConfig()


@lru_cache(maxsize=1)
def get_instance_config() -> InstanceConfig:
    """The process-wide instance config (read once; restart to change it)."""
    return load_instance_config()


def reset_instance_config_cache() -> None:
    get_instance_config.cache_clear()


def _main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "check":
        print("usage: python -m app.instance check <instance.json>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        cfg = parse_instance_config(path.read_text(encoding="utf-8"), source=str(path))
    except OSError as exc:
        print(f"{path}: cannot read ({exc.strerror})", file=sys.stderr)
        return 1
    except InstanceConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"ok: {path} (template '{cfg.default_template.name}', "
          f"{len(cfg.settings_defaults)} settings default(s), "
          f"footer fallback {'set' if cfg.footer_fallback_html else 'unset'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
