"""Contracts for the engine's workflows and the config-repo template.

Engine (``.github/workflows``): tests on every PR (the required check),
releases publish ``template.lock``, and nothing can reach Azure.

Config-repo template (``examples/config-repo``): merging to main deploys the
release pinned in ``template.lock``; the update check only ever rewrites
``template.lock``; the lock is verified against the engine's tag before use.
``tools/lock.sh`` is executed for real against a mock ``git``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE_WF = REPO_ROOT / ".github" / "workflows"
CFG = REPO_ROOT / "examples" / "config-repo"
CFG_WF = CFG / ".github" / "workflows"
PINNED_RUNNER = "ubuntu-26.04"


def load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    if True in data and "on" not in data:  # PyYAML reads `on` as True
        data["on"] = data.pop(True)
    return data


def all_workflows() -> list[Path]:
    return sorted(ENGINE_WF.glob("*.yml")) + sorted(CFG_WF.glob("*.yml"))


def steps(wf: dict, job: str) -> list[dict]:
    return wf["jobs"][job]["steps"]


def runs(wf: dict, job: str) -> str:
    return "\n".join(s.get("run", "") for s in steps(wf, job))


# ── every workflow ───────────────────────────────────────────────────────

@pytest.mark.parametrize("path", all_workflows(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_runner_and_actions_pinned(path):
    wf = load(path)
    for name, job in wf["jobs"].items():
        assert job["runs-on"] == PINNED_RUNNER, f"{name}: {job['runs-on']}"
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    for line in path.read_text().splitlines():
        if re.search(r"uses:\s*\S+@[0-9a-f]{40}", line):
            assert re.search(r"#\s*v\d+\.\d+\.\d+", line), line


@pytest.mark.parametrize("path", all_workflows(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_untrusted_expressions_in_shell(path):
    wf = load(path)
    for job in wf["jobs"].values():
        for step in job["steps"]:
            run = step.get("run", "")
            for bad in ("${{ inputs.", "${{ github.event.", "${{ github.head_ref", "${{ steps."):
                assert bad not in run, f"{step.get('name')}: {bad}"


@pytest.mark.parametrize("path", all_workflows(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_default_token_is_read_only(path):
    assert load(path)["permissions"] == {"contents": "read"}


# ── engine ───────────────────────────────────────────────────────────────

def test_engine_has_only_ci_and_release():
    assert sorted(p.name for p in ENGINE_WF.glob("*.yml")) == ["ci.yml", "release.yml"]


def test_engine_cannot_reach_azure():
    for path in ENGINE_WF.glob("*.yml"):
        text = path.read_text()
        assert "azure/login" not in text and "id-token" not in text, path.name


def test_ci_runs_tests_on_every_pr_unfiltered():
    wf = load(ENGINE_WF / "ci.yml")
    assert wf["on"]["pull_request"] == {"branches": ["main"]}
    assert wf["on"]["push"] == {"branches": ["main"]}
    assert "test" in wf["jobs"]


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_test_jobs_run_in_docker_and_fail_on_skips(name):
    r = runs(load(ENGINE_WF / name), "test")
    assert "docker build" in r
    assert "NLC_FAIL_ON_SKIP=1" in r and "zsh" in r and "pytest" in r


def test_ci_lints_engine_and_template_workflows():
    r = runs(load(ENGINE_WF / "ci.yml"), "actionlint")
    assert "rhysd/actionlint:" in r and "@sha256:" in r
    assert "examples/config-repo/.github/workflows/*.yml" in r


def test_release_is_tag_driven_tested_and_on_main():
    wf = load(ENGINE_WF / "release.yml")
    assert wf["on"] == {"push": {"tags": ["v*.*.*"]}}
    assert wf["jobs"]["release"]["needs"] == "test"
    assert wf["jobs"]["release"]["permissions"] == {"contents": "write"}
    r = runs(wf, "release")
    assert "merge-base --is-ancestor" in r
    assert "template.lock" in r and "gh release create" in r


# ── config-repo template ─────────────────────────────────────────────────

def test_template_has_deploy_validate_update_check():
    assert sorted(p.name for p in CFG_WF.glob("*.yml")) == ["deploy.yml", "update-check.yml", "validate.yml"]


def test_merge_to_main_deploys():
    wf = load(CFG_WF / "deploy.yml")
    assert wf["on"]["push"]["branches"] == ["main"]
    assert "template.lock" in wf["on"]["push"]["paths"]
    assert "workflow_dispatch" in wf["on"]


def test_deploy_job_is_environment_scoped_serialized_and_alone_holds_oidc():
    wf = load(CFG_WF / "deploy.yml")
    job = wf["jobs"]["deploy"]
    assert job["environment"] == "example"
    assert job["concurrency"]["cancel-in-progress"] is False
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    for other in ("validate.yml", "update-check.yml"):
        assert "id-token" not in (CFG_WF / other).read_text()


def test_deploy_checks_out_engine_at_verified_lock_sha():
    wf = load(CFG_WF / "deploy.yml")
    st = steps(wf, "deploy")
    lock = next(s for s in st if s.get("id") == "lock")
    assert lock["run"].startswith("tools/lock.sh")
    engine = next(s for s in st if s.get("with", {}).get("repository") == "pu-shd/nlcomposer")
    assert engine["with"]["ref"] == "${{ steps.lock.outputs.sha }}"
    assert st.index(lock) < st.index(engine)
    assert "ci-deploy.sh" in runs(wf, "deploy")


def test_validate_is_check_only_and_runs_on_prs():
    wf = load(CFG_WF / "validate.yml")
    assert "pull_request" in wf["on"] and "workflow_dispatch" in wf["on"]
    r = runs(wf, "validate")
    assert "--check-only" in r and "tools/lock.sh" in r


def test_update_check_only_rewrites_template_lock_and_dispatches_validate():
    wf = load(CFG_WF / "update-check.yml")
    r = runs(wf, "check")
    adds = re.findall(r"git add (.+)", r)
    assert adds == ["template.lock"]
    assert "gh workflow run validate.yml" in r
    assert "compare/" in r  # release commit must be on engine main
    assert "sort -V" in r  # never downgrade
    assert wf["jobs"]["check"]["permissions"] == {
        "contents": "write", "pull-requests": "write", "actions": "write",
    }


def test_example_unit_config_passes_engine_validation(tmp_path):
    r = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "ci-deploy.sh"),
         "--config", str(CFG / "example"), "--engine-ref", "v0.0.0", "--check-only"],
        env={**os.environ, "NLC_PYTHON": os.environ.get("PYTHON", "python3"),
             "ENGINE_SHA": "0" * 40, "ENGINE_ROOT": str(REPO_ROOT)},
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr


# ── tools/lock.sh against a mock git ─────────────────────────────────────

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def lock_env(tmp_path):
    if shutil.which("bash") is None:
        pytest.fail("bash required")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text('#!/usr/bin/env bash\n[ "$1" = ls-remote ] || exit 99\nprintf "%b" "$MOCK_LS_REMOTE"\n')
    git.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "tmp": tmp_path}


def run_lock(lock_env, lock: dict | str, ls_remote: str, **extra):
    p = lock_env["tmp"] / "template.lock"
    p.write_text(lock if isinstance(lock, str) else json.dumps(lock))
    env = {"PATH": lock_env["PATH"], "MOCK_LS_REMOTE": ls_remote, **extra}
    return subprocess.run(["bash", str(CFG / "tools" / "lock.sh"), str(p)],
                          env=env, capture_output=True, text=True, timeout=30)


GOOD = {"repo": "pu-shd/nlcomposer", "ref": "v1.0.0", "sha": SHA_A}


def test_lock_accepts_lightweight_tag(lock_env):
    r = run_lock(lock_env, GOOD, f"{SHA_A}\trefs/tags/v1.0.0\n")
    assert r.returncode == 0, r.stderr
    assert f"sha={SHA_A}" in r.stdout and "ref=v1.0.0" in r.stdout


def test_lock_uses_peeled_commit_of_annotated_tag(lock_env):
    r = run_lock(lock_env, GOOD, f"{SHA_B}\trefs/tags/v1.0.0\n{SHA_A}\trefs/tags/v1.0.0^{{}}\n")
    assert r.returncode == 0, r.stderr


def test_lock_writes_github_output(lock_env):
    out = lock_env["tmp"] / "out"
    r = run_lock(lock_env, GOOD, f"{SHA_A}\trefs/tags/v1.0.0\n", GITHUB_OUTPUT=str(out))
    assert r.returncode == 0, r.stderr
    assert out.read_text().splitlines() == ["ref=v1.0.0", f"sha={SHA_A}"]


@pytest.mark.parametrize("lock, remote, fragment", [
    (GOOD, f"{SHA_B}\trefs/tags/v1.0.0\n", "tag v1.0.0 is"),           # moved tag
    (GOOD, "", "not found"),                                           # missing tag
    ({**GOOD, "repo": "evil/nlcomposer"}, "", "is not pu-shd/nlcomposer"),
    ({**GOOD, "ref": "main"}, "", "not vX.Y.Z"),
    ({**GOOD, "sha": "abc"}, "", "not a full commit sha"),
    ("{not json", "", "not valid JSON"),
])
def test_lock_rejects(lock_env, lock, remote, fragment):
    r = run_lock(lock_env, lock, remote)
    assert r.returncode != 0
    assert fragment in r.stderr, r.stderr
