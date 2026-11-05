"""Contract tests for ``.github/workflows/deploy-sdc.yml``.

The workflow is the GitOps path to production: a push to ``main`` must
test, build, and roll a new Container App revision with no human input,
while the destructive ``wipe_database`` path must stay reachable only
through a manually-dispatched run with both confirmations typed.

The confirmation gate's shell is extracted and executed for real (with
the GitHub context supplied via env vars) so the gate's behavior is
tested, not just its text.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-sdc.yml"


@pytest.fixture(scope="module")
def wf() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML parses the bare key `on` as boolean True.
    if True in data and "on" not in data:
        data["on"] = data.pop(True)
    return data


def _steps(wf: dict, job: str) -> list[dict]:
    return wf["jobs"][job]["steps"]


def _step(wf: dict, job: str, name_prefix: str) -> dict:
    matches = [s for s in _steps(wf, job) if s.get("name", "").startswith(name_prefix)]
    assert len(matches) == 1, f"expected one step starting {name_prefix!r} in {job}, got {matches}"
    return matches[0]


def test_push_to_main_triggers_deploy(wf):
    push = wf["on"].get("push")
    assert push is not None, "workflow must trigger on push for GitOps"
    assert push.get("branches") == ["main"]


def test_manual_dispatch_still_available(wf):
    inputs = wf["on"]["workflow_dispatch"]["inputs"]
    for name in ("confirm", "image_tag", "wipe_database", "wipe_confirm", "file_share"):
        assert name in inputs


def test_docs_only_changes_do_not_deploy(wf):
    ignored = wf["on"]["push"].get("paths-ignore", [])
    assert "**.md" in ignored


def test_deploy_requires_tests_to_pass(wf):
    assert "test" in wf["jobs"], "a test job must exist"
    needs = wf["jobs"]["deploy"].get("needs")
    needs = [needs] if isinstance(needs, str) else needs
    assert needs == ["test"]


def test_tests_run_inside_docker(wf):
    runs = " ".join(s.get("run", "") for s in _steps(wf, "test"))
    assert "docker build" in runs
    assert re.search(r"docker run\b.*\bpytest\b", runs), runs


def test_deploys_are_serialized(wf):
    conc = wf.get("concurrency") or wf["jobs"]["deploy"].get("concurrency")
    assert conc, "concurrent deploys could race on the same Container App"
    assert conc.get("cancel-in-progress") is False


def test_wipe_step_only_reachable_from_manual_dispatch(wf):
    cond = _step(wf, "deploy", "Wipe app.db")["if"]
    assert "github.event_name == 'workflow_dispatch'" in cond
    assert "inputs.wipe_database == 'true'" in cond


def test_all_azure_secrets_are_target_suffixed(wf):
    text = WORKFLOW.read_text()
    names = set(re.findall(r"secrets\.([A-Z0-9_]+)", text))
    assert names, "workflow references no secrets?"
    unsuffixed = sorted(n for n in names if not n.endswith("_SDC"))
    assert unsuffixed == [], f"secrets missing _SDC suffix: {unsuffixed}"


def test_no_untrusted_expressions_inlined_into_shell(wf):
    """`${{ inputs.* }}` inside `run:` is a script-injection vector; pass via env."""
    for job in wf["jobs"].values():
        for step in job["steps"]:
            run = step.get("run", "")
            assert "${{ inputs." not in run, f"inline input in step {step.get('name')!r}"
            assert "${{ github.event." not in run, f"inline event data in step {step.get('name')!r}"


def test_image_tag_defaults_to_commit_sha(wf):
    env = wf["jobs"]["deploy"].get("env", {})
    assert "github.sha" in env.get("IMAGE_TAG", ""), env


# --- Execute the real gate script -------------------------------------------

def _run_gate(wf: dict, **env: str) -> subprocess.CompletedProcess:
    if shutil.which("bash") is None:
        pytest.fail("bash is required to exercise the deploy gate")
    step = _step(wf, "deploy", "Gate")
    full_env = {"PATH": os.environ["PATH"], **env}
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        env=full_env, capture_output=True, text=True, timeout=30,
    )


def test_gate_passes_on_push_without_inputs(wf):
    r = _run_gate(wf, EVENT_NAME="push", CONFIRM="", WIPE_DATABASE="", WIPE_CONFIRM="")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "gate passed" in r.stdout.lower()


def test_gate_rejects_dispatch_without_confirm(wf):
    r = _run_gate(wf, EVENT_NAME="workflow_dispatch", CONFIRM="", WIPE_DATABASE="false", WIPE_CONFIRM="")
    assert r.returncode != 0
    assert "confirm" in (r.stdout + r.stderr).lower()


def test_gate_accepts_dispatch_with_confirm(wf):
    r = _run_gate(wf, EVENT_NAME="workflow_dispatch", CONFIRM="deploy", WIPE_DATABASE="false", WIPE_CONFIRM="")
    assert r.returncode == 0, r.stdout + r.stderr


def test_gate_rejects_wipe_without_wipe_confirm(wf):
    r = _run_gate(wf, EVENT_NAME="workflow_dispatch", CONFIRM="deploy", WIPE_DATABASE="true", WIPE_CONFIRM="")
    assert r.returncode != 0
    assert "wipe" in (r.stdout + r.stderr).lower()


def test_gate_accepts_wipe_with_both_confirms(wf):
    r = _run_gate(wf, EVENT_NAME="workflow_dispatch", CONFIRM="deploy", WIPE_DATABASE="true", WIPE_CONFIRM="WIPE")
    assert r.returncode == 0, r.stdout + r.stderr
