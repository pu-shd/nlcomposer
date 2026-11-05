# nlcomposer config repo (template)

Deployment configuration for [`pu-shd/nlcomposer`](https://github.com/pu-shd/nlcomposer).
The engine is public and unit-agnostic; this private repo holds what one
department, unit or program actually runs: which engine release, which Azure
resources, and the newsletter's brand.

Convention: a config repo serving one unit lives in that unit's org
(`pu-<unit>/nlcomposer-config`); one serving several units lives in `pu-shd`.

## Layout

```text
template.lock                pinned engine release {repo, ref, sha}
<unit>/deploy.env            Azure targets (identifiers, never credentials)
<unit>/instance.json         brand: footer fallback, settings defaults, starter template
tools/lock.sh                verifies template.lock against the engine's tag
.github/workflows/
  deploy.yml                 merge to main → deploy (environment <unit>)
  validate.yml               required check: lock + config vs pinned engine
  update-check.yml           daily: newer engine release → PR changing template.lock
```

## How a change reaches production

| Change | How |
|---|---|
| Brand (`instance.json`) or targets (`deploy.env`) | PR → `validate` passes → merge → deploy |
| New engine release | update check opens "Pin engine vX.Y.Z" → merge → deploy |
| Re-deploy as-is | Actions → Deploy (GitOps) → Run workflow |

Deploys run `scripts/ci-deploy.sh` from the engine at the pinned commit: the
image is built in this unit's ACR (`<app>:<tag>-<sha7>`, reused if present),
a new revision is rolled with `NLC_INSTANCE_JSON`, and the job fails unless
that revision becomes healthy, is the one serving, and the custom domain
answers. A failed revision never takes traffic; the previous one keeps serving.

## Starting a new unit

1. Create `pu-<unit>/nlcomposer-config` (private) from this directory; rename
   `example/` to `<unit>/` and `example` to `<unit>` in `.github/workflows/`.
2. Set `template.lock` from the latest engine release's `template.lock` asset.
3. Fill `<unit>/deploy.env` and `<unit>/instance.json`.
4. From an engine checkout:
   `scripts/deploy.sh setup --config ../nlcomposer-config/<unit>` provisions
   Azure, the CI managed identity (federated to the subject GitHub reports
   for the repo plus `:environment:<unit>`), the GitHub
   Environment and the `AZURE_*` repository variables.
5. Settings → Actions → General: allow GitHub Actions to create pull requests
   (for the update check).
6. Protect `main`: require a PR and the `validate` check.

## Security

- No secrets in the repo or in GitHub. Azure login is OIDC to a managed
  identity with Contributor on this unit's resource group only, usable only
  from the `<unit>` Environment, which only deploys from protected branches.
- App secrets (e.g. `youtube-api-key`) are Container App secrets, referenced
  by the app and never passed through GitHub.
- `tools/lock.sh` refuses a lock whose sha differs from the engine tag, so a
  moved tag cannot change what deploys.
