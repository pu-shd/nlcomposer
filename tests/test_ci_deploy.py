"""``scripts/ci-deploy.sh`` against mock ``az`` and ``curl``.

The script is the whole production rollout for every config repo, so each
gate is exercised for real: config parsing and validation happen before any
Azure call, the image is reused when its tag exists, the new revision must be
healthy *and* serving, and the custom-domain smoke test must pass.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "ci-deploy.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

MOCK_AZ = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MOCK_LOG"
case "$1 $2" in
  "acr repository") echo "${MOCK_TAG_EXISTS:-0}" ;;
  "acr build") exit "${MOCK_BUILD_RC:-0}" ;;
  "containerapp update")
    for a in "$@"; do
      if [ "$prev" = "--revision-suffix" ]; then echo "$a" > "$MOCK_DIR/suffix"; fi
      prev="$a"
    done ;;
  "containerapp revision") printf '%b\n' "${MOCK_STATE:-RunningAtMaxScale\tHealthy}" ;;
  "containerapp show")
    if [ -n "${MOCK_LATEST:-}" ]; then echo "$MOCK_LATEST"; else echo "app--$(cat "$MOCK_DIR/suffix")"; fi ;;
  *) echo "mock az: unexpected: $*" >&2; exit 99 ;;
esac
"""

MOCK_CURL = r"""#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$MOCK_LOG"
printf '%s' "${MOCK_HTTP:-401}"
"""


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("az", MOCK_AZ), ("curl", MOCK_CURL)):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "deploy.env").write_text(textwrap.dedent("""\
        # targets
        AZ_SUBSCRIPTION="ignored-by-ci"
        AZ_RG="rg-example"
        AZ_ACR="acrexample"
        AZ_ACA_APP="app"
        AZ_CUSTOM_DOMAIN="news.example.edu"
        """))
    (cfg / "instance.json").write_text(json.dumps({
        "footer_fallback_html": '<p style="margin:0">Maintained by Example = Editor.</p>',
        "default_template": {"name": "Example Default"},
    }, indent=2))
    e = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "MOCK_LOG": str(tmp_path / "calls.log"),
        "MOCK_DIR": str(tmp_path),
        "ENGINE_ROOT": str(REPO_ROOT),
        "ENGINE_SHA": SHA,
        "NLC_PYTHON": sys.executable,
        "NLC_POLL_SECONDS": "0",
        "NLC_POLL_MAX": "3",
        "GITHUB_RUN_NUMBER": "7",
    }
    return {"env": e, "cfg": cfg, "log": tmp_path / "calls.log", "tmp": tmp_path}


def run(env, *args, **extra) -> subprocess.CompletedProcess:
    e = {**env["env"], **extra}
    return subprocess.run(
        ["bash", str(SCRIPT), "--config", str(env["cfg"]), "--engine-ref", "v1.2.3", *args],
        env=e, capture_output=True, text=True, timeout=60,
    )


def calls(env) -> list[str]:
    return env["log"].read_text().splitlines() if env["log"].exists() else []


def az_calls(env) -> list[str]:
    return [c for c in calls(env) if not c.startswith("curl ")]


# ── happy path ───────────────────────────────────────────────────────────

def test_full_rollout_builds_rolls_waits_and_smokes(env):
    r = run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    c = calls(env)
    assert any(x.startswith("acr build --registry acrexample --image app:v1.2.3-0123456") for x in c)
    update = next(x for x in c if x.startswith("containerapp update"))
    assert "--image acrexample.azurecr.io/app:v1.2.3-0123456" in update
    assert "--revision-suffix v1-2-3-r7" in update
    assert "-g rg-example" in update
    assert any(x.startswith("containerapp revision show") and "--revision app--v1-2-3-r7" in x for x in c)
    assert any(x.startswith("curl ") and "https://news.example.edu/" in x for x in c)


def test_instance_json_is_passed_compact_and_intact(env):
    r = run(env)
    assert r.returncode == 0, r.stderr
    update = next(x for x in calls(env) if x.startswith("containerapp update"))
    payload = update.split("NLC_INSTANCE_JSON=", 1)[1].split(" --output")[0]
    assert json.loads(payload)["footer_fallback_html"].endswith("Example = Editor.</p>")
    assert ": " not in payload  # compact


def test_existing_image_tag_is_reused(env):
    r = run(env, MOCK_TAG_EXISTS="1")
    assert r.returncode == 0, r.stderr
    assert not any(x.startswith("acr build") for x in calls(env))
    assert "reusing" in r.stdout


def test_no_custom_domain_skips_smoke_and_still_succeeds(env):
    p = env["cfg"] / "deploy.env"
    p.write_text("\n".join(l for l in p.read_text().splitlines() if "CUSTOM_DOMAIN" not in l))
    summary = env["tmp"] / "summary.md"
    r = run(env, GITHUB_STEP_SUMMARY=str(summary))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any(x.startswith("curl ") for x in calls(env))
    assert "revision" in summary.read_text()


def test_step_summary_written(env):
    summary = env["tmp"] / "summary.md"
    r = run(env, GITHUB_STEP_SUMMARY=str(summary))
    assert r.returncode == 0, r.stderr
    text = summary.read_text()
    assert "v1.2.3" in text and "app--v1-2-3-r7" in text and "401" in text


# ── gates that must stop a rollout ───────────────────────────────────────

@pytest.mark.parametrize("state", [r"Failed\tUnhealthy", r"Running\tUnhealthy", r"Degraded\tNone"])
def test_unhealthy_revision_fails(env, state):
    r = run(env, MOCK_STATE=state)
    assert r.returncode != 0
    assert "previous revision keeps serving" in r.stderr


def test_revision_that_never_becomes_healthy_times_out(env):
    r = run(env, MOCK_STATE=r"Activating\tNone")
    assert r.returncode != 0
    assert "not healthy" in r.stderr


def test_revision_not_serving_fails(env):
    r = run(env, MOCK_LATEST="app--something-else")
    assert r.returncode != 0
    assert "latest ready revision" in r.stderr


@pytest.mark.parametrize("code", ["500", "502", "000", "404"])
def test_failed_smoke_test_fails(env, code):
    r = run(env, MOCK_HTTP=code)
    assert r.returncode != 0
    assert code in r.stderr


def test_failed_image_build_stops_before_rollout(env):
    r = run(env, MOCK_BUILD_RC="1")
    assert r.returncode != 0
    assert not any(x.startswith("containerapp update") for x in calls(env))


# ── config validation happens before any Azure call ──────────────────────

def _assert_rejected_before_azure(env, r, fragment):
    assert r.returncode != 0, r.stdout
    assert fragment in r.stderr, r.stderr
    assert az_calls(env) == []


def test_invalid_instance_json_rejected_before_azure(env):
    (env["cfg"] / "instance.json").write_text(json.dumps({"surprise": True}))
    _assert_rejected_before_azure(env, run(env), "failed validation")


def test_missing_instance_json_rejected(env):
    (env["cfg"] / "instance.json").unlink()
    _assert_rejected_before_azure(env, run(env), "instance.json not found")


@pytest.mark.parametrize("line, fragment", [
    ('AZ_RG="rg; rm -rf /"', "outside"),
    ("AZ_ACR=$(whoami)", "outside"),
    ("not a pair", "not KEY=VALUE"),
])
def test_unsafe_deploy_env_rejected(env, line, fragment):
    p = env["cfg"] / "deploy.env"
    p.write_text(p.read_text() + line + "\n")
    _assert_rejected_before_azure(env, run(env), fragment)


def test_missing_required_key_rejected(env):
    p = env["cfg"] / "deploy.env"
    p.write_text("\n".join(l for l in p.read_text().splitlines() if not l.startswith("AZ_ACR=")))
    _assert_rejected_before_azure(env, run(env), "AZ_ACR is required")


def test_deploy_env_is_never_sourced(env):
    marker = env["tmp"] / "pwned"
    p = env["cfg"] / "deploy.env"
    p.write_text(p.read_text() + f"OTHER_KEY=$(touch {marker})\n")
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert not marker.exists()


@pytest.mark.parametrize("ref", ["", "v1 2", "../x", "-v1"])
def test_bad_engine_ref_rejected(env, ref):
    e = {**env["env"]}
    r = subprocess.run(["bash", str(SCRIPT), "--config", str(env["cfg"]), "--engine-ref", ref],
                       env=e, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert az_calls(env) == []


# ── modes ────────────────────────────────────────────────────────────────

def test_check_only_validates_without_azure(env):
    r = run(env, "--check-only")
    assert r.returncode == 0, r.stderr
    assert "configuration is valid" in r.stdout
    assert calls(env) == []


def test_dry_run_makes_no_calls(env):
    r = run(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "would build" in r.stdout
    assert calls(env) == []
