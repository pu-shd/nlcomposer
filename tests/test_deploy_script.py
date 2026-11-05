"""Smoke tests for ``scripts/deploy.sh`` configuration handling.

These don't talk to Azure. They invoke the script with ``--dry-run --yes``
and a synthetic ``DEPLOY_ENV_FILE`` to assert that:

* The saved configuration is honored end-to-end (the (P)var indirection
  bug that ate the user's saved values is regression-tested directly).
* ``--yes`` without a config file refuses to proceed, instead of falling
  through to built-in defaults — which historically caused a second
  resource set to be provisioned in the wrong region.
* The Container App definition munge that adds the file-share volume
  copes with ACA's habit of serializing unset arrays as JSON ``null``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy.sh"


def _has_zsh() -> bool:
    return shutil.which("zsh") is not None


pytestmark = pytest.mark.skipif(
    not _has_zsh(), reason="deploy.sh requires zsh; not installed in this env"
)


def _run_deploy(args, *, env=None) -> subprocess.CompletedProcess:
    """Invoke deploy.sh in dry-run mode with a clean PATH and no Azure auth."""
    base_env = {
        # Keep PATH so zsh + gh + az resolve; az is invoked but DRY_RUN
        # makes the script print commands rather than execute them, so
        # it's fine if `az login` was never run.
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        # Some preflight steps probe `gh auth status`; we can't suppress
        # that here, but the dry-run path short-circuits before any
        # network call. Keep stdin closed so any read() bails fast.
    }
    if env:
        base_env.update(env)
    return subprocess.run(
        [str(DEPLOY_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=base_env,
        cwd=str(REPO_ROOT),
        timeout=60,
        check=False,
    )


def test_yes_without_env_file_refuses_to_run(tmp_path):
    """Defense-in-depth: --yes is meant for unattended re-runs of an
    already-saved config. Without one, the script previously fell
    through to built-in defaults and provisioned a second resource
    set in eastus. Refuse instead of guessing."""
    env_path = tmp_path / "missing.env"
    assert not env_path.exists()
    res = _run_deploy(
        ["setup", "--yes", "--dry-run"],
        env={"DEPLOY_ENV_FILE": str(env_path)},
    )
    assert res.returncode == 2, res.stdout + res.stderr
    combined = res.stdout + res.stderr
    assert "--yes requires an existing" in combined
    assert str(env_path) in combined


def test_yes_with_env_file_honors_saved_values(tmp_path):
    """The (P)var indirection bug used to clobber sourced values with
    built-in defaults whenever --yes ran. Pinned: distinctive values
    in the env file must surface verbatim in the configuration
    summary — never the defaults."""
    env_path = tmp_path / "deploy.env"
    env_path.write_text(
        '\n'.join([
            'AZ_SUBSCRIPTION="my-test-subscription-XYZ"',
            'AZ_LOCATION="canadacentral"',
            'AZ_RG="rg-pinned-from-env"',
            'AZ_STORAGE="ststickyenvabc99"',
            'AZ_FILE_SHARE="sticky-share"',
            'AZ_ACR="acrstickyenvabc99"',
            'AZ_ACA_ENV="cae-sticky-env"',
            'AZ_ACA_APP="sticky-app"',
            'AZ_IMAGE_TAG="vTEST"',
            'AZ_ENTRA_APP="Sticky Auth Test"',
            'GH_REPO="example/sticky-repo"',
            '',
        ])
    )

    res = _run_deploy(
        ["setup", "--yes", "--dry-run"],
        env={"DEPLOY_ENV_FILE": str(env_path)},
    )
    combined = res.stdout + res.stderr

    # The configuration summary must reflect what's in the env file.
    # Every distinctive token below would NOT be present if (P)var
    # came back empty and prompt_with_default fell through to defaults.
    for needle in (
        "my-test-subscription-XYZ",
        "canadacentral",
        "rg-pinned-from-env",
        "ststickyenvabc99",
        "sticky-share",
        "acrstickyenvabc99",
        "cae-sticky-env",
        "sticky-app",
        "vTEST",
        "Sticky Auth Test",
        "example/sticky-repo",
    ):
        assert needle in combined, (
            f"saved value {needle!r} missing from script output — "
            f"the (P)var indirection regression has come back.\n\n"
            f"Output:\n{combined}"
        )

    # And the built-in defaults that previously leaked through must
    # NOT appear (they would only show up if the bug re-emerged).
    for default_leak in (
        "rg-newsletter-composer",
        "stnlcomposer",   # default storage-name prefix
        "acrnlcomposer",  # default ACR prefix
        "cae-newsletter-composer",
    ):
        assert default_leak not in combined, (
            f"built-in default {default_leak!r} leaked into output "
            f"despite a saved env file — regression of the --yes bug.\n\n"
            f"Output:\n{combined}"
        )


# ───────── Volume-bind JSON munge ────────────────────────────────────


# Inline copy of the JSON mutation that ``scripts/deploy.sh`` runs to
# add the sdc-data-mount volume + mountpoint. Kept in sync with the
# heredoc inside the shell script so the regressions are testable
# without invoking azure CLI.
_VOLUME_BIND_PY = textwrap.dedent(
    """
    import json, pathlib, sys
    p = pathlib.Path(sys.argv[1])
    doc = json.loads(p.read_text())
    props = doc.setdefault("properties", {})
    tmpl = props.get("template") or {}
    props["template"] = tmpl
    containers = tmpl.get("containers") or []
    if containers:
        mounts = containers[0].get("volumeMounts") or []
        if not any(m.get("volumeName") == "sdc-data-mount" for m in mounts):
            mounts.append({"volumeName": "sdc-data-mount", "mountPath": "/app/data"})
        containers[0]["volumeMounts"] = mounts
    volumes = tmpl.get("volumes") or []
    if not any(v.get("name") == "sdc-data-mount" for v in volumes):
        volumes.append({
            "name": "sdc-data-mount",
            "storageType": "AzureFile",
            "storageName": "sdc-data",
        })
    tmpl["volumes"] = volumes
    p.write_text(json.dumps(doc, indent=2))
    """
)


def _run_volume_bind(tmp_path: Path, payload: dict) -> dict:
    src = tmp_path / "aca.json"
    src.write_text(json.dumps(payload))
    script = tmp_path / "munge.py"
    script.write_text(_VOLUME_BIND_PY)
    res = subprocess.run(
        ["python3", str(script), str(src)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(src.read_text())


def test_volume_bind_handles_null_volumes_field(tmp_path):
    """ACA serializes an unset ``volumes`` array as ``null``. The
    earlier munge used ``setdefault("volumes", [])`` which preserved
    the None and crashed at the next ``any()`` iteration."""
    out = _run_volume_bind(tmp_path, {
        "properties": {
            "template": {
                "containers": [{"name": "c", "image": "x", "volumeMounts": None}],
                "volumes": None,
            }
        }
    })
    tmpl = out["properties"]["template"]
    assert tmpl["volumes"] == [{
        "name": "sdc-data-mount",
        "storageType": "AzureFile",
        "storageName": "sdc-data",
    }]
    assert tmpl["containers"][0]["volumeMounts"] == [
        {"volumeName": "sdc-data-mount", "mountPath": "/app/data"},
    ]


def test_volume_bind_is_idempotent(tmp_path):
    """A second pass on an already-bound app is a no-op (no duplicate
    entries) — the script reruns this step on every `setup`."""
    pre_bound = {
        "properties": {
            "template": {
                "containers": [{
                    "name": "c", "image": "x",
                    "volumeMounts": [{"volumeName": "sdc-data-mount", "mountPath": "/app/data"}],
                }],
                "volumes": [{
                    "name": "sdc-data-mount",
                    "storageType": "AzureFile",
                    "storageName": "sdc-data",
                }],
            }
        }
    }
    out = _run_volume_bind(tmp_path, pre_bound)
    tmpl = out["properties"]["template"]
    assert len(tmpl["volumes"]) == 1
    assert len(tmpl["containers"][0]["volumeMounts"]) == 1


def test_volume_bind_preserves_existing_unrelated_volumes(tmp_path):
    """Adding sdc-data-mount mustn't drop a sibling volume the user
    happens to have configured — protects against future expansion
    where multiple file-share mounts coexist."""
    out = _run_volume_bind(tmp_path, {
        "properties": {
            "template": {
                "containers": [{
                    "name": "c", "image": "x",
                    "volumeMounts": [{"volumeName": "logs", "mountPath": "/app/logs"}],
                }],
                "volumes": [{
                    "name": "logs",
                    "storageType": "EmptyDir",
                }],
            }
        }
    })
    names = {v["name"] for v in out["properties"]["template"]["volumes"]}
    assert names == {"logs", "sdc-data-mount"}


# ───────── Configuration prompts ─────────────────────────────────────


def test_principals_prompt_round_trips_through_env_file(tmp_path):
    """AZ_ENTRA_PRINCIPALS must be sourced and re-saved by write_env_file
    so the Entra-restriction step has a value to act on."""
    env_path = tmp_path / "deploy.env"
    env_path.write_text(
        '\n'.join([
            'AZ_SUBSCRIPTION="sub-x"',
            'AZ_LOCATION="canadacentral"',
            'AZ_RG="rg-x"',
            'AZ_STORAGE="sticky11"',
            'AZ_FILE_SHARE="sticky-share"',
            'AZ_ACR="acrsticky11"',
            'AZ_ACA_ENV="cae-sticky"',
            'AZ_ACA_APP="sticky-app"',
            'AZ_IMAGE_TAG="vTEST"',
            'AZ_ENTRA_APP="Sticky Auth Test"',
            'AZ_ENTRA_PRINCIPALS="alice@example.com,Engineering Team"',
            'GH_REPO="example/sticky"',
            '',
        ])
    )
    res = _run_deploy(
        ["setup", "--yes", "--dry-run"],
        env={"DEPLOY_ENV_FILE": str(env_path)},
    )
    combined = res.stdout + res.stderr
    assert "alice@example.com,Engineering Team" in combined
    # Re-saved env file preserves the value verbatim.
    assert 'AZ_ENTRA_PRINCIPALS="alice@example.com,Engineering Team"' in env_path.read_text()

# ───────── Config-repo layout + managed-identity CI wiring ───────────


_MOCK_CLI = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$MOCK_LOG"
case "$(basename "$0") $1 $2" in
  "az group exists") echo true ;;
esac
exit 0
"""


def _mock_path(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("az", "gh"):
        p = bin_dir / name
        p.write_text(_MOCK_CLI)
        p.chmod(0o755)
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "MOCK_LOG": str(tmp_path / "calls.log"),
    }


def _unit_dir(tmp_path: Path) -> Path:
    unit = tmp_path / "cfgrepo" / "unitx"
    unit.mkdir(parents=True)
    (unit / "deploy.env").write_text("\n".join([
        'AZ_SUBSCRIPTION="sub-unitx"',
        'AZ_LOCATION="canadacentral"',
        'AZ_RG="rg-unitx"',
        'AZ_STORAGE="stunitx"',
        'AZ_FILE_SHARE="unitx-data"',
        'AZ_ACR="acrunitx"',
        'AZ_ACA_ENV="cae-unitx"',
        'AZ_ACA_APP="unitx-app"',
        'AZ_IMAGE_TAG="v1"',
        'AZ_ENTRA_APP="UnitX Auth"',
        'AZ_ENTRA_PRINCIPALS=""',
        'AZ_ENTRA_MINIMAL_PERMS="true"',
        'AZ_CUSTOM_DOMAIN="news.unitx.example.edu"',
        'AZ_CI_IDENTITY="id-unitx-ci"',
        'GH_REPO="pu-unitx/nlcomposer-config"',
        'GH_ENVIRONMENT="unitx"',
        "",
    ]))
    (unit / "instance.json").write_text('{"default_template": {"name": "UnitX Default"}}')
    return unit


def test_config_flag_reads_unit_deploy_env(tmp_path):
    unit = _unit_dir(tmp_path)
    res = _run_deploy(["setup", "--config", str(unit), "--yes", "--dry-run"], env=_mock_path(tmp_path))
    out = res.stdout + res.stderr
    for needle in ("rg-unitx", "id-unitx-ci", "pu-unitx/nlcomposer-config", "environment unitx",
                   "news.unitx.example.edu"):
        assert needle in out, f"{needle!r} missing:\n{out}"
    assert "Target Suffix" not in out


def test_setup_wires_ci_as_managed_identity_scoped_to_environment(tmp_path):
    unit = _unit_dir(tmp_path)
    res = _run_deploy(["setup", "--config", str(unit), "--yes", "--dry-run"], env=_mock_path(tmp_path))
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "az identity create -n id-unitx-ci -g rg-unitx" in out
    assert "--subject repo:pu-unitx/nlcomposer-config:environment:unitx" in out
    assert "gh variable set AZURE_CLIENT_ID --repo pu-unitx/nlcomposer-config" in out
    assert "repos/pu-unitx/nlcomposer-config/environments/unitx" in out
    # The old path: an app-registration SP, branch-scoped subject, secrets.
    assert "gh secret set" not in out
    assert "az ad app create --display-name sp-" not in out
    assert "ref:refs/heads/main" not in out


def test_update_requires_config_dir(tmp_path):
    env_file = tmp_path / "deploy.env"
    env_file.write_text('AZ_RG="rg"\n')
    res = _run_deploy(["update", "--yes", "--dry-run"],
                      env={**_mock_path(tmp_path), "DEPLOY_ENV_FILE": str(env_file)})
    assert res.returncode == 2
    assert "--config" in res.stderr


def test_update_delegates_to_ci_deploy(tmp_path):
    unit = _unit_dir(tmp_path)
    env = {
        **_mock_path(tmp_path),
        "ENGINE_SHA": "0123456789abcdef0123456789abcdef01234567",
        "NLC_PYTHON": sys.executable,
    }
    res = _run_deploy(["update", "--config", str(unit), "--yes", "--dry-run"], env=env)
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "acrunitx.azurecr.io/unitx-app:dev-0123456" in out
    assert "dry-run: would build" in out
