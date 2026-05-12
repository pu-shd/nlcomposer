#!/usr/bin/env zsh
# Newsletter Composer — Azure deployment CLI
#
# Walks through setup / update / teardown of an idempotent Azure deployment:
#   - Resource Group
#   - Azure Container Registry (ACR)
#   - Azure Storage Account + File Share (persistent SQLite)
#   - Azure Container Apps environment + Container App
#   - Entra ID app registration (built-in auth for the Container App)
#   - GitHub OIDC federated credential + repo secrets for CI/CD
#
# Designed for macOS zsh. Requires `az`, `gh`, and (for local builds) `docker`.
# Where automation cannot drive a step (portal-only consents, GitHub App
# install screens), the script pauses with explicit manual instructions.

set -euo pipefail
setopt PIPE_FAIL ERR_RETURN 2>/dev/null || true

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
# DEPLOY_ENV_FILE override exists for tests + power users who want to
# keep multiple stage configs side-by-side. Defaults to .deploy.env in
# the repo root for the normal one-environment-per-checkout case.
ENV_FILE="${DEPLOY_ENV_FILE:-${REPO_ROOT}/.deploy.env}"
SUBSCRIPTION=""
ASSUME_YES=0
DRY_RUN=0
ACTION=""

# ───── colors ──────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_RESET=$'\e[0m'
  C_BOLD=$'\e[1m'
  C_DIM=$'\e[2m'
  C_RED=$'\e[31m'
  C_GREEN=$'\e[32m'
  C_YELLOW=$'\e[33m'
  C_BLUE=$'\e[34m'
  C_MAGENTA=$'\e[35m'
  C_CYAN=$'\e[36m'
else
  C_RESET="" C_BOLD="" C_DIM="" C_RED="" C_GREEN="" C_YELLOW="" C_BLUE="" C_MAGENTA="" C_CYAN=""
fi

banner() {
  print -P "${C_MAGENTA}${C_BOLD}"
  cat <<'EOF'
  ┌──────────────────────────────────────────────────────────────┐
  │  Newsletter Composer — Azure deploy CLI                     │
  │  Container Apps · Azure Files · Entra ID · GitHub OIDC      │
  └──────────────────────────────────────────────────────────────┘
EOF
  print -P "${C_RESET}"
}

step()    { print -P "${C_CYAN}${C_BOLD}▸ $*${C_RESET}"; }
ok()      { print -P "${C_GREEN}  ✓ $*${C_RESET}"; }
info()    { print -P "${C_DIM}  · $*${C_RESET}"; }
warn()    { print -P "${C_YELLOW}  ⚠ $*${C_RESET}"; }
err()     { print -P "${C_RED}${C_BOLD}  ✗ $*${C_RESET}" >&2; }
heading() { print -P "\n${C_BLUE}${C_BOLD}== $* ==${C_RESET}"; }

# Pause for a manual / portal-only step.
pause_for_manual() {
  local title="$1"; shift
  print -P "\n${C_YELLOW}${C_BOLD}⏸  Manual step: ${title}${C_RESET}"
  for line in "$@"; do
    print -P "${C_YELLOW}     • ${line}${C_RESET}"
  done
  if (( ASSUME_YES )); then
    info "(auto-yes) skipping interactive pause"
    return 0
  fi
  print -nP "${C_YELLOW}${C_BOLD}   Press ENTER once complete (Ctrl-C to abort)…${C_RESET} "
  read -r _ || true
}

confirm() {
  local prompt="${1:-Continue?}"
  if (( ASSUME_YES )); then
    return 0
  fi
  print -nP "${C_YELLOW}${prompt} [y/N] ${C_RESET}"
  local reply
  read -r reply || reply=""
  [[ "$reply" == [yY] || "$reply" == [yY][eE][sS] ]]
}

run() {
  if (( DRY_RUN )); then
    print -P "${C_DIM}    \$ $*${C_RESET}"
    return 0
  fi
  "$@"
}

run_capture() {
  if (( DRY_RUN )); then
    print -P "${C_DIM}    \$ $*${C_RESET}" >&2
    print "DRYRUN"
    return 0
  fi
  "$@"
}

# Read-only query that returns a placeholder under --dry-run so dependent
# steps can still execute without real resources existing.
query() {
  if (( DRY_RUN )); then
    print -P "${C_DIM}    ? $*${C_RESET}" >&2
    print "DRYRUN-VALUE"
    return 0
  fi
  "$@"
}

# ───── env file ────────────────────────────────────────────────────────
write_env_file() {
  cat > "$ENV_FILE" <<EOF
# Newsletter Composer — Azure deployment env
# Sourced by scripts/deploy.sh. Safe to edit; rerun \`deploy.sh setup\`.
AZ_SUBSCRIPTION="${AZ_SUBSCRIPTION}"
AZ_LOCATION="${AZ_LOCATION}"
AZ_RG="${AZ_RG}"
AZ_STORAGE="${AZ_STORAGE}"
AZ_FILE_SHARE="${AZ_FILE_SHARE}"
AZ_ACR="${AZ_ACR}"
AZ_ACA_ENV="${AZ_ACA_ENV}"
AZ_ACA_APP="${AZ_ACA_APP}"
AZ_IMAGE_TAG="${AZ_IMAGE_TAG}"
AZ_ENTRA_APP="${AZ_ENTRA_APP}"
AZ_ENTRA_PRINCIPALS="${AZ_ENTRA_PRINCIPALS}"
AZ_ENTRA_MINIMAL_PERMS="${AZ_ENTRA_MINIMAL_PERMS}"
GH_REPO="${GH_REPO}"
EOF
  ok "Saved configuration to ${ENV_FILE}"
}

prompt_with_default() {
  local var="$1" prompt="$2" default="$3"
  # NOTE: keep the (P)var indirection on its own line. When combined with
  # `local var="$1" ... current="${(P)var:-}"` on a single line, zsh
  # resolves (P)var BEFORE the new value of $var lands in the local
  # scope — so $current came back empty even though the global was set
  # from .deploy.env, and `--yes` happily overwrote the saved config
  # with the built-in defaults. Splitting the assignments keeps the
  # existing-value path working.
  local current="${(P)var:-}"
  local effective="${current:-$default}"
  if (( ASSUME_YES )); then
    typeset -g "$var"="$effective"
    return 0
  fi
  print -nP "${C_CYAN}${prompt}${C_RESET} ${C_DIM}[${effective}]${C_RESET}: "
  local reply
  read -r reply || reply=""
  typeset -g "$var"="${reply:-$effective}"
}

ensure_config() {
  heading "Configuration"

  if [[ -f "$ENV_FILE" ]]; then
    info "Loading existing config from ${ENV_FILE}"
    # shellcheck disable=SC1090
    source "$ENV_FILE"
  elif (( ASSUME_YES )); then
    # Defense-in-depth: --yes is intended for unattended re-runs of an
    # already-saved configuration. Without a .deploy.env, prompt_with_default
    # falls back to BUILT-IN defaults (e.g. rg-newsletter-composer in
    # eastus), which is almost never what an unattended caller wants —
    # and historically led to a second, accidental resource set being
    # provisioned. Refuse rather than guess.
    err "--yes requires an existing ${ENV_FILE}."
    err "Run \`scripts/deploy.sh setup\` interactively first to seed the file."
    exit 2
  fi

  local default_repo=""
  if command -v gh >/dev/null 2>&1; then
    default_repo=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)
  fi
  local short_user="${USER:-dev}"
  local stamp="${RANDOM}${RANDOM}"
  stamp="${stamp:0:5}"

  prompt_with_default AZ_SUBSCRIPTION   "Azure subscription (id or name)"     ""
  prompt_with_default AZ_LOCATION       "Azure region"                        "eastus"
  prompt_with_default AZ_RG             "Resource group"                      "rg-newsletter-composer"
  prompt_with_default AZ_STORAGE        "Storage account name (3-24 lower)"   "stnlcomposer${short_user:0:6}${stamp}"
  prompt_with_default AZ_FILE_SHARE     "Azure Files share name"              "sdc-data"
  prompt_with_default AZ_ACR            "Container registry name"             "acrnlcomposer${short_user:0:6}${stamp}"
  prompt_with_default AZ_ACA_ENV        "Container Apps environment"          "cae-newsletter-composer"
  prompt_with_default AZ_ACA_APP        "Container App name"                  "newsletter-composer"
  prompt_with_default AZ_IMAGE_TAG      "Image tag"                           "v1"
  prompt_with_default AZ_ENTRA_APP      "Entra ID app display name"           "Newsletter Composer (auth)"
  prompt_with_default AZ_ENTRA_PRINCIPALS \
                      "Allowed principals (comma-separated UPNs / group names / object IDs; empty = tenant-wide)" \
                      ""
  prompt_with_default AZ_ENTRA_MINIMAL_PERMS \
                      "Strip Graph permissions from Entra app (avoid admin-consent prompt for OIDC sign-in)? [true/false]" \
                      "true"
  prompt_with_default GH_REPO           "GitHub repo (owner/name) for CI"     "${default_repo}"

  # Lower-case the storage and ACR names to satisfy Azure naming rules.
  AZ_STORAGE="${AZ_STORAGE:l}"
  AZ_ACR="${AZ_ACR:l}"

  print
  info "Subscription : ${AZ_SUBSCRIPTION:-<current az account>}"
  info "Region       : ${AZ_LOCATION}"
  info "Resource grp : ${AZ_RG}"
  info "Storage acct : ${AZ_STORAGE}"
  info "File share   : ${AZ_FILE_SHARE}"
  info "ACR          : ${AZ_ACR}"
  info "ACA env / app: ${AZ_ACA_ENV} / ${AZ_ACA_APP}"
  info "Image tag    : ${AZ_IMAGE_TAG}"
  info "Entra app    : ${AZ_ENTRA_APP}"
  info "Allowed      : ${AZ_ENTRA_PRINCIPALS:-<tenant-wide>}"
  info "Minimal perms: ${AZ_ENTRA_MINIMAL_PERMS} (clear requiredResourceAccess on app reg)"
  info "GitHub repo  : ${GH_REPO:-<not set>}"

  confirm "Save and continue with these values?" || { err "Aborted by user."; exit 1; }
  write_env_file
}

load_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    err "Missing ${ENV_FILE}. Run \`scripts/deploy.sh setup\` first."
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$ENV_FILE"
}

# ───── preflight ───────────────────────────────────────────────────────
need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    err "Missing required command: ${C_BOLD}$1${C_RESET}. ${2:-}"
    return 1
  fi
}

preflight() {
  heading "Preflight"
  local missing=0
  need_cmd az "Install: brew install azure-cli" || missing=1
  need_cmd gh "Install: brew install gh"        || missing=1
  need_cmd docker "Install Docker Desktop (only required for local image builds)" || warn "docker missing — will use \`az acr build\` server-side"
  (( missing )) && exit 1

  if ! az account show >/dev/null 2>&1; then
    warn "Not logged into Azure CLI."
    if confirm "Run \`az login\` now?"; then
      run az login --use-device-code
    else
      err "Azure login required."; exit 1
    fi
  fi
  ok "Azure CLI logged in: $(az account show --query user.name -o tsv 2>/dev/null || echo unknown)"

  if [[ -n "${AZ_SUBSCRIPTION:-}" ]]; then
    run az account set --subscription "$AZ_SUBSCRIPTION"
    ok "Subscription set: ${AZ_SUBSCRIPTION}"
  fi

  if ! gh auth status >/dev/null 2>&1; then
    warn "GitHub CLI not authenticated."
    if confirm "Run \`gh auth login\` now?"; then
      run gh auth login
    else
      warn "Skipping — repo secrets / federated credentials must be configured manually."
    fi
  else
    ok "GitHub CLI logged in: $(gh api user -q .login 2>/dev/null || echo unknown)"
  fi

  step "Registering Azure resource providers (idempotent)"
  for ns in Microsoft.App Microsoft.OperationalInsights Microsoft.ContainerRegistry Microsoft.Storage; do
    run az provider register --namespace "$ns" >/dev/null
  done
  ok "Providers registered"
}

# ───── resource steps ──────────────────────────────────────────────────
ensure_resource_group() {
  step "Resource group: ${AZ_RG}"
  if [[ "$(az group exists -n "$AZ_RG" 2>/dev/null || echo false)" == "true" ]]; then
    ok "Already exists"
  else
    run az group create -n "$AZ_RG" -l "$AZ_LOCATION" >/dev/null
    ok "Created"
  fi
}

ensure_storage() {
  step "Storage account: ${AZ_STORAGE}"
  if az storage account show -n "$AZ_STORAGE" -g "$AZ_RG" >/dev/null 2>&1; then
    ok "Already exists"
  else
    local available
    available=$(az storage account check-name --name "$AZ_STORAGE" --query nameAvailable -o tsv 2>/dev/null || echo "false")
    if [[ "$available" != "true" ]]; then
      err "Storage name ${AZ_STORAGE} unavailable. Pick a different name in ${ENV_FILE}."
      exit 1
    fi
    run az storage account create \
      -n "$AZ_STORAGE" -g "$AZ_RG" -l "$AZ_LOCATION" \
      --sku Standard_LRS --kind StorageV2 \
      --allow-blob-public-access false >/dev/null
    ok "Created"
  fi

  step "File share: ${AZ_FILE_SHARE}"
  if az storage share-rm show \
       --resource-group "$AZ_RG" --storage-account "$AZ_STORAGE" \
       --name "$AZ_FILE_SHARE" >/dev/null 2>&1; then
    ok "Already exists"
  else
    run az storage share-rm create \
      --resource-group "$AZ_RG" --storage-account "$AZ_STORAGE" \
      --name "$AZ_FILE_SHARE" --quota 5 --enabled-protocols SMB >/dev/null
    ok "Created (5 GiB quota)"
  fi
}

ensure_acr() {
  step "Container registry: ${AZ_ACR}"
  if az acr show -n "$AZ_ACR" -g "$AZ_RG" >/dev/null 2>&1; then
    ok "Already exists"
  else
    local available
    available=$(az acr check-name --name "$AZ_ACR" --query nameAvailable -o tsv 2>/dev/null || echo "false")
    if [[ "$available" != "true" ]]; then
      err "ACR name ${AZ_ACR} unavailable. Pick a different name in ${ENV_FILE}."
      exit 1
    fi
    run az acr create -n "$AZ_ACR" -g "$AZ_RG" --sku Basic --admin-enabled false >/dev/null
    ok "Created"
  fi
}

build_and_push_image() {
  step "Build & push image: ${AZ_ACR}.azurecr.io/${AZ_ACA_APP}:${AZ_IMAGE_TAG}"
  # Prefer az acr build (server-side, no local docker daemon needed).
  run az acr build \
    --registry "$AZ_ACR" \
    --image "${AZ_ACA_APP}:${AZ_IMAGE_TAG}" \
    --file Dockerfile \
    "$REPO_ROOT" >/dev/null
  ok "Image pushed"
}

ensure_log_workspace() {
  step "Log Analytics workspace (for ACA env)"
  local ws_name="log-${AZ_ACA_ENV}"
  if az monitor log-analytics workspace show -n "$ws_name" -g "$AZ_RG" >/dev/null 2>&1; then
    ok "Already exists: $ws_name"
  else
    run az monitor log-analytics workspace create -n "$ws_name" -g "$AZ_RG" -l "$AZ_LOCATION" >/dev/null
    ok "Created: $ws_name"
  fi
  LAW_ID=$(query az monitor log-analytics workspace show -n "$ws_name" -g "$AZ_RG" --query customerId -o tsv 2>/dev/null || echo "")
  LAW_KEY=$(query az monitor log-analytics workspace get-shared-keys -n "$ws_name" -g "$AZ_RG" --query primarySharedKey -o tsv 2>/dev/null || echo "")
}

ensure_aca_env() {
  step "Container Apps environment: ${AZ_ACA_ENV}"
  if az containerapp env show -n "$AZ_ACA_ENV" -g "$AZ_RG" >/dev/null 2>&1; then
    ok "Already exists"
  else
    run az containerapp env create \
      -n "$AZ_ACA_ENV" -g "$AZ_RG" -l "$AZ_LOCATION" \
      --logs-workspace-id "$LAW_ID" \
      --logs-workspace-key "$LAW_KEY" >/dev/null
    ok "Created"
  fi

  step "Mount Azure Files share as ACA storage"
  local storage_key
  storage_key=$(query az storage account keys list -g "$AZ_RG" -n "$AZ_STORAGE" --query "[0].value" -o tsv)
  local mount_name="sdc-data"
  if az containerapp env storage show -n "$AZ_ACA_ENV" -g "$AZ_RG" --storage-name "$mount_name" >/dev/null 2>&1; then
    ok "Already linked: $mount_name"
  else
    run az containerapp env storage set \
      --name "$AZ_ACA_ENV" --resource-group "$AZ_RG" \
      --storage-name "$mount_name" \
      --azure-file-account-name "$AZ_STORAGE" \
      --azure-file-account-key "$storage_key" \
      --azure-file-share-name "$AZ_FILE_SHARE" \
      --access-mode ReadWrite >/dev/null
    ok "Linked"
  fi
}

ensure_container_app() {
  step "Container app: ${AZ_ACA_APP}"
  local image="${AZ_ACR}.azurecr.io/${AZ_ACA_APP}:${AZ_IMAGE_TAG}"

  if az containerapp show -n "$AZ_ACA_APP" -g "$AZ_RG" >/dev/null 2>&1; then
    info "Updating existing container app to image ${image}"
    # --revision-suffix forces a brand-new revision even when the image
    # tag string is unchanged across runs. Without it `az containerapp
    # update --image` compares the template by literal value, finds
    # nothing different, and silently no-ops — the new ACR digest sits
    # there while the previous container keeps serving. A timestamp
    # suffix is unique per invocation and small enough to fit ACA's
    # 64-char revision-name budget.
    local rev_suffix
    rev_suffix="r$(date -u +%Y%m%d%H%M%S)"
    run az containerapp update \
      -n "$AZ_ACA_APP" -g "$AZ_RG" \
      --image "$image" \
      --revision-suffix "$rev_suffix" \
      --set-env-vars "SDC_DATA_DIR=/app/data" "SDC_SQLITE_VFS=unix-dotfile" >/dev/null
    ok "Updated (new revision suffix ${rev_suffix})"
  else
    run az containerapp create \
      -n "$AZ_ACA_APP" -g "$AZ_RG" \
      --environment "$AZ_ACA_ENV" \
      --image "$image" \
      --target-port 8000 --ingress external \
      --registry-server "${AZ_ACR}.azurecr.io" \
      --registry-identity system \
      --system-assigned \
      --env-vars "SDC_DATA_DIR=/app/data" "SDC_SQLITE_VFS=unix-dotfile" \
      --min-replicas 1 --max-replicas 1 \
      --cpu 0.5 --memory 1.0Gi >/dev/null
    ok "Created"
  fi
  # SDC_SQLITE_VFS=unix-dotfile is required when SDC_DATA_DIR points
  # at an Azure Files SMB mount: SQLite's default POSIX advisory locks
  # fail on this filesystem ("database is locked" on every PRAGMA).
  # The dotfile VFS coordinates writers via a sentinel <db>-journal
  # dotfile, which any filesystem can create/delete atomically. WAL is
  # also incompatible — the app's connect() falls back to the default
  # rollback journal automatically. See app/db.py.

  step "Bind file-share volume to /app/data"
  if (( DRY_RUN )); then
    info "(dry-run) would export JSON, inject sdc-data-mount volume + mount, re-apply"
  else
    # ACA has no convenient CLI patch for binding a file-share volume,
    # so we export the app definition, mutate it, and re-apply via
    # `containerapp update --yaml`. The flag accepts both YAML and JSON
    # — JSON is what `az ... show -o json` emits and what Python's
    # stdlib can parse without PyYAML, keeping this step dependency-free.
    local tmpjson; tmpjson=$(mktemp -t aca-XXXX.json)
    az containerapp show -n "$AZ_ACA_APP" -g "$AZ_RG" -o json > "$tmpjson"
    if grep -q '"sdc-data-mount"' "$tmpjson" 2>/dev/null; then
      ok "Volume already bound"
    else
      local py_bin
      if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
        py_bin="${REPO_ROOT}/.venv/bin/python"
      else
        py_bin="$(command -v python3 || true)"
      fi
      if [[ -z "$py_bin" ]]; then
        err "Need python3 (with stdlib json) to mutate the ACA definition; not found in PATH."
        rm -f "$tmpjson"
        return 1
      fi
      "$py_bin" - "$tmpjson" <<'PY'
import json, pathlib, sys

# ACA's JSON serializes empty / unset arrays as `null`, not as missing
# keys — so dict.setdefault is the wrong tool (it preserves None). We
# coalesce with `or []` and then explicitly write the result back.
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
PY
      run az containerapp update -n "$AZ_ACA_APP" -g "$AZ_RG" --yaml "$tmpjson" >/dev/null
      ok "Volume bound"
    fi
    rm -f "$tmpjson"
  fi

  step "Grant Container App pull rights on ACR"
  local app_principal
  app_principal=$(query az containerapp show -n "$AZ_ACA_APP" -g "$AZ_RG" --query identity.principalId -o tsv 2>/dev/null || echo "")
  local acr_id
  acr_id=$(query az acr show -n "$AZ_ACR" -g "$AZ_RG" --query id -o tsv 2>/dev/null || echo "")
  if [[ -n "$app_principal" && -n "$acr_id" ]]; then
    if (( ! DRY_RUN )) && az role assignment list --assignee "$app_principal" --scope "$acr_id" --role AcrPull --query "[0].id" -o tsv 2>/dev/null | grep -q .; then
      ok "AcrPull already granted"
    else
      run az role assignment create --assignee "$app_principal" --role AcrPull --scope "$acr_id" >/dev/null
      ok "Granted AcrPull"
    fi
  fi

  local fqdn
  fqdn=$(query az containerapp show -n "$AZ_ACA_APP" -g "$AZ_RG" --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || echo "")
  ok "App URL: https://${fqdn:-<pending>}"
  APP_FQDN="$fqdn"
}

ensure_entra_app() {
  step "Entra ID app registration: ${AZ_ENTRA_APP}"
  local app_id existing
  existing=$(query az ad app list --display-name "$AZ_ENTRA_APP" --query "[0].appId" -o tsv 2>/dev/null || echo "")
  if (( DRY_RUN )); then existing=""; fi
  if [[ -n "$existing" ]]; then
    app_id="$existing"
    ok "Already exists (appId ${app_id})"
  else
    app_id=$(run_capture az ad app create \
      --display-name "$AZ_ENTRA_APP" \
      --sign-in-audience AzureADMyOrg \
      --web-redirect-uris "https://${APP_FQDN}/.auth/login/aad/callback" \
      --query appId -o tsv)
    ok "Created (appId ${app_id})"
  fi
  ENTRA_APP_ID="$app_id"

  # Enable id_token issuance. The Container Apps auth proxy uses the
  # OIDC hybrid flow (`response_type=code+id_token`); without
  # `web.implicitGrantSettings.enableIdTokenIssuance=true` Microsoft
  # rejects sign-in with AADSTS700054. The CLI's
  # `az ad app update --set web.implicitGrantSettings.*` is broken
  # for nested Graph properties, so we PATCH directly. Idempotent.
  if (( ! DRY_RUN )); then
    local _obj_id
    _obj_id=$(az ad app show --id "$app_id" --query id -o tsv 2>/dev/null || echo "")
    if [[ -n "$_obj_id" ]]; then
      az rest --method PATCH \
        --uri "https://graph.microsoft.com/v1.0/applications/${_obj_id}" \
        --headers "Content-Type=application/json" \
        --body '{"web":{"implicitGrantSettings":{"enableIdTokenIssuance":true,"enableAccessTokenIssuance":false}}}' \
        >/dev/null 2>&1 \
        || warn "Failed to enable id_token issuance — sign-in may fail with AADSTS700054 until this is set."
    fi
  fi

  # Strip Graph permissions (User.Read, offline_access, …) from the app
  # registration when AZ_ENTRA_MINIMAL_PERMS=true. Container Apps Easy
  # Auth only needs `openid` for sign-in + named-principal gating; the
  # default-template permissions trigger a consent screen that, in
  # tenants which disable user consent (e.g. universities), is gated
  # behind admin approval and blocks every login. Clearing the array
  # makes the consent screen vanish entirely.
  if (( ! DRY_RUN )) && [[ "${AZ_ENTRA_MINIMAL_PERMS:-true}" == "true" ]] && [[ -n "${_obj_id:-}" ]]; then
    step "Strip Graph permissions from Entra app reg (skip consent prompt)"
    if az rest --method PATCH \
         --uri "https://graph.microsoft.com/v1.0/applications/${_obj_id}" \
         --headers "Content-Type=application/json" \
         --body '{"requiredResourceAccess": []}' \
         >/dev/null 2>&1; then
      ok "Cleared requiredResourceAccess"
    else
      warn "Failed to clear requiredResourceAccess — admin consent may still be required."
    fi
  fi

  # Repair / extend the redirect URI list. Historically the create-new
  # branch baked in whatever APP_FQDN was at create-time — if the
  # Container App hadn't been provisioned yet (e.g. a failed first
  # run), the URI was malformed and silently broke the auth callback
  # until someone went to the portal. Make every setup re-verify the
  # callback is registered, preserving any other URIs the operator
  # may have added manually.
  if [[ -n "${APP_FQDN:-}" ]] && (( ! DRY_RUN )); then
    local want="https://${APP_FQDN}/.auth/login/aad/callback"
    local have_uris
    have_uris=$(az ad app show --id "$app_id" --query "web.redirectUris" -o tsv 2>/dev/null || echo "")
    if print -- "$have_uris" | grep -Fxq "$want"; then
      info "Redirect URI already registered: ${want}"
    else
      # Drop any malformed http(s)://.auth/... entries from a botched
      # earlier run; keep everything else and add the canonical URI.
      local merged
      merged=$(print -- "$have_uris" | awk -v WANT="$want" '
        $0 ~ /^https?:\/\/\/?\.auth\// { next }   # discard malformed
        $0 != ""                       { print }
        END                            { print WANT }
      ' | tr '\n' ' ')
      info "Updating redirect URIs on app reg"
      run az ad app update --id "$app_id" --web-redirect-uris ${=merged} >/dev/null
      ok "Redirect URI registered: ${want}"
    fi
  fi

  # The Application object alone can't host App Role Assignments — that
  # surface lives on the ServicePrincipal in the tenant. Create one if
  # the auth flow hasn't already (idempotent: --id is the appId, not
  # the SP id, so a re-run is a no-op when the SP already exists).
  if (( ! DRY_RUN )); then
    az ad sp create --id "$ENTRA_APP_ID" >/dev/null 2>&1 || true
  fi

  # Mint a fresh client secret automatically via Graph (same as the portal
  # "New client secret" button) and immediately stash it on the Container
  # App, so setup is fully unattended end-to-end. The plaintext value is
  # only available in this single response — we capture it, push it into
  # the auth secret store, and discard it. No copy-paste round-trip
  # through a human.
  local client_secret=""
  if (( ! DRY_RUN )); then
    step "Mint Entra app client secret (1-year, auto-rotated each setup)"
    client_secret=$(az ad app credential reset \
      --id "$ENTRA_APP_ID" \
      --append \
      --years 1 \
      --display-name "deploy-script-$(date -u +%Y%m%d)" \
      --query password -o tsv 2>/dev/null || echo "")
    if [[ -n "$client_secret" ]]; then
      ok "Secret minted"
    else
      warn "Could not mint client secret via Graph — falling back to manual paste."
      pause_for_manual "Generate Entra app client secret" \
        "Open https://portal.azure.com → Entra ID → App registrations → ${AZ_ENTRA_APP}" \
        "Certificates & secrets → New client secret (1-year recommended)" \
        "Copy the secret VALUE — you will paste it next."
      if (( ! ASSUME_YES )); then
        print -nP "${C_CYAN}Paste client-secret value:${C_RESET} "
        read -rs client_secret || client_secret=""
        print
      fi
    fi
  fi

  if (( ASSUME_YES )) && [[ -z "$client_secret" ]]; then
    info "(auto-yes + no secret) skipping client-secret bind"
  else
    if [[ -n "$client_secret" ]]; then
      step "Bind Entra auth to Container App"
      # We cannot use `az containerapp auth microsoft update` here.
      # That command's update_aad_settings() function calls
      # prompt_y_n() with default="n" before doing the bind, and the
      # underlying knack.prompting.verify_is_a_tty() raises a hard
      # NoTTYException whenever stdin isn't a TTY — which is true the
      # moment we either pipe input in or run the script under any
      # non-interactive harness. Piping `y\n` doesn't help (the TTY
      # check fires before the read); there is no global --yes that
      # silences this one. Instead, write the secret via
      # `az containerapp secret set` (no prompt) and then PUT the
      # authConfig directly through the management API via `az rest`
      # (also no prompt), bypassing the CLI subcommand entirely.
      if (( DRY_RUN )); then
        info "(dry-run) would store secret + PUT authConfig on $AZ_ACA_APP"
      else
        local tenant_id
        tenant_id=$(az account show --query tenantId -o tsv)
        local sub_id
        sub_id=$(az account show --query id -o tsv)

        # Stash the client secret on the Container App as a named
        # secret. The authConfig references it by name (not by value)
        # so secret rotation later means only re-running this step.
        az containerapp secret set \
          -n "$AZ_ACA_APP" -g "$AZ_RG" \
          --secrets "microsoft-provider-authentication-secret=${client_secret}" >/dev/null

        # Build + send the authConfig payload. Using a tempfile keeps
        # the secret off the command line (which would show up in
        # `ps`) — though here only the *name* is in the payload, the
        # tempfile pattern is also less brittle for any quoting fun
        # with the JSON.
        local auth_uri="https://management.azure.com/subscriptions/${sub_id}/resourceGroups/${AZ_RG}/providers/Microsoft.App/containerApps/${AZ_ACA_APP}/authConfigs/current?api-version=2024-03-01"
        # When MINIMAL_PERMS is on we also override loginParameters so
        # Easy Auth requests only `openid` from AAD. Without this, the
        # platform's built-in defaults add `profile email offline_access
        # User.Read`, which trigger the consent screen even when the
        # app registration has no requiredResourceAccess declared.
        local login_block=""
        if [[ "${AZ_ENTRA_MINIMAL_PERMS:-true}" == "true" ]]; then
          login_block=',
        "login": {"loginParameters": ["scope=openid"]}'
        fi
        local body
        body=$(cat <<EOF
{
  "properties": {
    "platform": {"enabled": true},
    "globalValidation": {
      "unauthenticatedClientAction": "RedirectToLoginPage",
      "redirectToProvider": "azureactivedirectory"
    },
    "identityProviders": {
      "azureActiveDirectory": {
        "registration": {
          "openIdIssuer": "https://login.microsoftonline.com/${tenant_id}/v2.0",
          "clientId": "${ENTRA_APP_ID}",
          "clientSecretSettingName": "microsoft-provider-authentication-secret"
        }${login_block}
      }
    }
  }
}
EOF
)
        az rest --method PUT \
          --uri "$auth_uri" \
          --headers "Content-Type=application/json" \
          --body "$body" >/dev/null

        # The running revision was launched before the auth secret
        # existed — its embedded auth sidecar can't materialize the
        # clientSecretSettingName value, so the post-login callback
        # silently fails with a blank page. Force a new revision so
        # the secret gets baked into the next pod.
        local active_rev
        active_rev=$(az containerapp revision list \
          -n "$AZ_ACA_APP" -g "$AZ_RG" \
          --query "[?properties.active].name | [0]" -o tsv 2>/dev/null || echo "")
        if [[ -n "$active_rev" ]]; then
          az containerapp revision restart \
            -n "$AZ_ACA_APP" -g "$AZ_RG" \
            --revision "$active_rev" >/dev/null \
            || warn "Revision restart failed; new auth secret may not load until next deploy."
        fi
      fi
      ok "Auth enabled (redirect-to-login)"
    else
      warn "No secret provided — auth left in current state."
    fi
  fi
}

# Resolve a single principal token (UPN, group name, or object ID) to
# {object_id, type}. Echoes "<id>|<type>" on stdout where type is one
# of User / Group; empty echo + non-zero return when unresolvable.
# Logs (via warn) so the caller can keep going on partial failures.
resolve_principal() {
  local token="$1"
  local out=""
  # UUID — assume it's already an object ID, sniff for type.
  if [[ "$token" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    if az ad user show --id "$token" --query id -o tsv >/dev/null 2>&1; then
      print -n -- "${token}|User"
      return 0
    fi
    if az ad group show --group "$token" --query id -o tsv >/dev/null 2>&1; then
      print -n -- "${token}|Group"
      return 0
    fi
    warn "Object ID ${token} is neither a user nor a group in this tenant — skipping."
    return 1
  fi
  # UPN-style — looks like an email.
  if [[ "$token" == *@* ]]; then
    out=$(az ad user show --id "$token" --query id -o tsv 2>/dev/null || echo "")
    if [[ -n "$out" ]]; then
      print -n -- "${out}|User"
      return 0
    fi
    warn "User ${token} not found — skipping."
    return 1
  fi
  # Otherwise: group display name.
  out=$(az ad group show --group "$token" --query id -o tsv 2>/dev/null || echo "")
  if [[ -n "$out" ]]; then
    print -n -- "${out}|Group"
    return 0
  fi
  warn "Group ${token} not found — skipping."
  return 1
}

ensure_entra_principal_assignments() {
  step "Restrict access to allowed Entra principals"
  if [[ -z "${ENTRA_APP_ID:-}" ]]; then
    warn "Entra app id missing — run ensure_entra_app first."
    return 0
  fi
  if [[ -z "${AZ_ENTRA_PRINCIPALS:-}" ]]; then
    info "AZ_ENTRA_PRINCIPALS empty — leaving the app open to any tenant member."
    info "Set this in .deploy.env (comma-separated) and rerun to lock down access."
    return 0
  fi

  if (( DRY_RUN )); then
    info "(dry-run) would set appRoleAssignmentRequired=true on the Entra SP and assign:"
    print -- "$AZ_ENTRA_PRINCIPALS" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | grep -v '^$' | while read -r p; do
      info "  · ${p}"
    done
    return 0
  fi

  local sp_id
  sp_id=$(az ad sp show --id "$ENTRA_APP_ID" --query id -o tsv 2>/dev/null || echo "")
  if [[ -z "$sp_id" ]]; then
    warn "Entra service principal not found for appId ${ENTRA_APP_ID} — try rerunning setup."
    return 0
  fi

  # Flip appRoleAssignmentRequired=true so unassigned users get a
  # consent/access-denied page instead of silently signing in.
  info "Setting appRoleAssignmentRequired=true on the Entra SP"
  az rest \
    --method PATCH \
    --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${sp_id}" \
    --headers "Content-Type=application/json" \
    --body '{"appRoleAssignmentRequired": true}' >/dev/null 2>&1 \
    || warn "Failed to set appRoleAssignmentRequired (insufficient Graph perms?)."

  # Snapshot existing assignments so we don't double-add on rerun. The
  # "default access" appRole id (00000000-...) is what gets used when
  # an assignment is created without specifying a custom role.
  local existing_ids
  existing_ids=$(az rest \
    --method GET \
    --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${sp_id}/appRoleAssignedTo?\$select=principalId" \
    --query 'value[].principalId' -o tsv 2>/dev/null || echo "")

  local resolved="" tok pid_type pid ptype assigned_count=0 skipped_count=0
  print -- "$AZ_ENTRA_PRINCIPALS" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | grep -v '^$' | while read -r tok; do
    pid_type=$(resolve_principal "$tok" 2>/dev/null) || { skipped_count=$((skipped_count+1)); continue; }
    pid="${pid_type%|*}"
    ptype="${pid_type#*|}"
    if print -- "$existing_ids" | grep -q "^${pid}$"; then
      info "Already assigned: ${tok} (${ptype})"
      continue
    fi
    info "Assigning ${tok} (${ptype})"
    az rest \
      --method POST \
      --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${sp_id}/appRoleAssignedTo" \
      --headers "Content-Type=application/json" \
      --body "{\"principalId\":\"${pid}\",\"resourceId\":\"${sp_id}\",\"appRoleId\":\"00000000-0000-0000-0000-000000000000\"}" \
      >/dev/null 2>&1 \
      && assigned_count=$((assigned_count+1)) \
      || warn "Failed to assign ${tok} — may need elevated Graph permissions."
  done

  # Pretty summary — list everyone currently assigned (post-update),
  # so the operator can sanity-check who has access.
  ok "Allowed principals on ${AZ_ENTRA_APP}:"
  local rows
  rows=$(az rest \
    --method GET \
    --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${sp_id}/appRoleAssignedTo?\$select=principalDisplayName,principalType" \
    --query 'value[].[principalDisplayName,principalType]' -o tsv 2>/dev/null || echo "")
  if [[ -z "$rows" ]]; then
    warn "  (none — sign-in will be blocked until at least one assignment lands)"
  else
    print -- "$rows" | while IFS=$'\t' read -r name type; do
      info "  · ${name} (${type})"
    done
  fi
}

ensure_github_oidc() {
  if [[ -z "${GH_REPO:-}" ]]; then
    warn "GH_REPO empty — skipping GitHub OIDC wiring."
    return 0
  fi
  step "GitHub OIDC service principal for CI"

  local sp_name="sp-${AZ_ACA_APP}-cicd"
  local sp_id
  sp_id=$(query az ad app list --display-name "$sp_name" --query "[0].appId" -o tsv 2>/dev/null || echo "")
  if (( DRY_RUN )); then sp_id=""; fi
  if [[ -z "$sp_id" ]]; then
    sp_id=$(run_capture az ad app create --display-name "$sp_name" --query appId -o tsv)
    run az ad sp create --id "$sp_id" >/dev/null 2>&1 || true
    ok "Created service principal: ${sp_name} (${sp_id})"
  else
    ok "Service principal exists: ${sp_name} (${sp_id})"
  fi

  local sub_id rg_id
  sub_id=$(query az account show --query id -o tsv)
  rg_id=$(query az group show -n "$AZ_RG" --query id -o tsv)
  if (( ! DRY_RUN )) && az role assignment list --assignee "$sp_id" --scope "$rg_id" --role Contributor --query "[0].id" -o tsv 2>/dev/null | grep -q .; then
    ok "Contributor on RG already granted"
  else
    run az role assignment create --assignee "$sp_id" --role Contributor --scope "$rg_id" >/dev/null
    ok "Contributor on RG granted"
  fi

  step "Federated credential for ${GH_REPO}@refs/heads/main"
  local fed_name="gh-${AZ_ACA_APP}-main"
  local fed_exists
  fed_exists=$(query az ad app federated-credential list --id "$sp_id" --query "[?name=='${fed_name}'].name" -o tsv 2>/dev/null || echo "")
  if (( DRY_RUN )); then fed_exists=""; fi
  if [[ -n "$fed_exists" ]]; then
    ok "Federated credential already exists"
  else
    local tmpfc; tmpfc=$(mktemp -t fc-XXXX.json)
    cat > "$tmpfc" <<EOF
{
  "name": "${fed_name}",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:${GH_REPO}:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}
EOF
    run az ad app federated-credential create --id "$sp_id" --parameters "@${tmpfc}" >/dev/null
    rm -f "$tmpfc"
    ok "Federated credential created"
  fi

  step "Set GitHub repo secrets via gh CLI"
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    local tenant_id; tenant_id=$(query az account show --query tenantId -o tsv)
    run gh secret set AZURE_CLIENT_ID       --repo "$GH_REPO" --body "$sp_id"
    run gh secret set AZURE_TENANT_ID       --repo "$GH_REPO" --body "$tenant_id"
    run gh secret set AZURE_SUBSCRIPTION_ID --repo "$GH_REPO" --body "$sub_id"
    run gh secret set AZURE_RESOURCE_GROUP  --repo "$GH_REPO" --body "$AZ_RG"
    run gh secret set AZURE_ACR_NAME        --repo "$GH_REPO" --body "$AZ_ACR"
    run gh secret set AZURE_ACA_APP         --repo "$GH_REPO" --body "$AZ_ACA_APP"
    ok "Secrets set on ${GH_REPO}"
  else
    pause_for_manual "Set GitHub repo secrets manually" \
      "AZURE_CLIENT_ID=${sp_id}" \
      "AZURE_TENANT_ID=$(query az account show --query tenantId -o tsv 2>/dev/null || echo '<tenant>')" \
      "AZURE_SUBSCRIPTION_ID=${sub_id}" \
      "AZURE_RESOURCE_GROUP=${AZ_RG}" \
      "AZURE_ACR_NAME=${AZ_ACR}" \
      "AZURE_ACA_APP=${AZ_ACA_APP}"
  fi

  pause_for_manual "(Optional) GitHub App for richer CI/CD" \
    "If you prefer a GitHub App (vs Actions OIDC) for deploys:" \
    "1. https://github.com/settings/apps/new — name 'sdc-composer-deploy'" \
    "2. Permissions: contents:read, actions:write, deployments:write" \
    "3. Install on ${GH_REPO} and copy the App ID + private key" \
    "4. Store the private key as repo secret DEPLOY_APP_PRIVATE_KEY"
}

# ───── teardown ────────────────────────────────────────────────────────
teardown() {
  load_env
  heading "Teardown"
  warn "This will DELETE the resource group ${AZ_RG} (and everything in it),"
  warn "the Entra app ${AZ_ENTRA_APP}, and the CI service principal."
  if ! confirm "Are you absolutely sure?"; then
    err "Aborted."; exit 1
  fi
  print -nP "${C_RED}${C_BOLD}Type the resource group name to confirm: ${C_RESET}"
  if (( ! ASSUME_YES )); then
    local typed
    read -r typed || typed=""
    if [[ "$typed" != "$AZ_RG" ]]; then
      err "Mismatch — abort."; exit 1
    fi
  fi

  step "Deleting resource group ${AZ_RG} (async)"
  run az group delete -n "$AZ_RG" --yes --no-wait || warn "RG delete failed (may not exist)"
  ok "Delete dispatched"

  local id
  for app in "$AZ_ENTRA_APP" "sp-${AZ_ACA_APP}-cicd"; do
    id=$(az ad app list --display-name "$app" --query "[0].id" -o tsv 2>/dev/null || echo "")
    if [[ -n "$id" ]]; then
      step "Deleting Entra app: $app"
      run az ad app delete --id "$id" || warn "Could not delete $app"
      ok "Deleted"
    else
      info "Entra app not found (already gone): $app"
    fi
  done

  if [[ -n "${GH_REPO:-}" ]] && command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    step "Removing GitHub repo secrets"
    for s in AZURE_CLIENT_ID AZURE_TENANT_ID AZURE_SUBSCRIPTION_ID AZURE_RESOURCE_GROUP AZURE_ACR_NAME AZURE_ACA_APP; do
      run gh secret delete "$s" --repo "$GH_REPO" 2>/dev/null || true
    done
    ok "Secrets removed (where present)"
  fi

  pause_for_manual "Verify GitHub App / portal cleanup" \
    "If you created a GitHub App for deploys, uninstall it from ${GH_REPO:-<repo>}." \
    "Confirm the resource group eventually disappears in the portal."

  ok "Teardown complete (RG delete continues asynchronously)."
}

# ───── status ──────────────────────────────────────────────────────────
status() {
  load_env
  heading "Status"
  printf "  %-22s %s\n" "Resource group"  "$(az group exists -n "$AZ_RG" 2>/dev/null || echo unknown)"
  printf "  %-22s %s\n" "Storage account" "$(az storage account show -n "$AZ_STORAGE" -g "$AZ_RG" --query name -o tsv 2>/dev/null || echo missing)"
  printf "  %-22s %s\n" "ACR"             "$(az acr show -n "$AZ_ACR" -g "$AZ_RG" --query loginServer -o tsv 2>/dev/null || echo missing)"
  printf "  %-22s %s\n" "ACA env"         "$(az containerapp env show -n "$AZ_ACA_ENV" -g "$AZ_RG" --query name -o tsv 2>/dev/null || echo missing)"
  printf "  %-22s %s\n" "Container App"   "$(az containerapp show -n "$AZ_ACA_APP" -g "$AZ_RG" --query name -o tsv 2>/dev/null || echo missing)"
  local fqdn; fqdn=$(az containerapp show -n "$AZ_ACA_APP" -g "$AZ_RG" --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || echo "")
  if [[ -n "$fqdn" ]]; then
    printf "  %-22s %s\n" "App URL" "https://${fqdn}"
  fi
}

# ───── orchestration ───────────────────────────────────────────────────
do_setup() {
  banner
  ensure_config
  preflight
  heading "Provision Azure resources"
  ensure_resource_group
  ensure_storage
  ensure_acr
  build_and_push_image
  ensure_log_workspace
  ensure_aca_env
  ensure_container_app
  heading "Identity & auth"
  ensure_entra_app
  ensure_entra_principal_assignments
  heading "CI/CD"
  ensure_github_oidc
  heading "Done"
  ok "Setup complete."
  info "Run \`scripts/deploy.sh status\` to inspect, or \`scripts/deploy.sh update\` to redeploy."
}

do_update() {
  load_env
  preflight
  heading "Build & redeploy"
  warn "This rolls a new container revision against ${AZ_ACA_APP} in"
  warn "${AZ_RG}. The Azure Files share ${AZ_FILE_SHARE} (where app.db"
  warn "lives) is NOT touched — drafts, settings, archive, and the"
  warn "media library persist across this rollout. Schema changes that"
  warn "are NOT forward-compatible require an explicit \`deploy.sh"
  warn "wipe-data\` first."
  if ! confirm "Build the image and roll the new revision?"; then
    err "Aborted."; exit 1
  fi
  build_and_push_image
  ensure_container_app
  ok "Update complete."
}

# Wipe the SQLite database on the Azure Files share. Surface is
# intentionally separate from `update` so a code rollout never destroys
# data by accident. Required when a schema change is NOT forward-
# compatible and the app has no production data worth migrating yet.
do_wipe_data() {
  load_env
  preflight
  banner
  heading "Wipe SQLite database on Azure Files share"
  warn "This will DELETE app.db from share '${AZ_FILE_SHARE}' in storage"
  warn "account '${AZ_STORAGE}'. ALL drafts, settings, templates, archive"
  warn "entries, and uploaded media are lost. Cached image bytes in the"
  warn "media library are also gone."
  warn ""
  warn "Use this only when an incompatible schema change has shipped and"
  warn "the app has no production data worth keeping. To preserve data,"
  warn "abort now and migrate the schema instead."
  if ! confirm "Continue?"; then
    err "Aborted."; exit 1
  fi
  if (( ! ASSUME_YES )); then
    print -nP "${C_RED}${C_BOLD}Type the file share name '${AZ_FILE_SHARE}' to confirm: ${C_RESET}"
    local typed
    read -r typed || typed=""
    if [[ "$typed" != "$AZ_FILE_SHARE" ]]; then
      err "Mismatch — abort."; exit 1
    fi
  fi

  step "Resolving storage key"
  local key
  key=$(query az storage account keys list -g "$AZ_RG" -n "$AZ_STORAGE" \
        --query "[0].value" -o tsv 2>/dev/null || echo "")
  if [[ -z "$key" ]]; then
    err "Could not read storage key — is the storage account up?"
    exit 1
  fi

  # The running container holds an open SQLite handle on app.db over the
  # SMB share. Deleting an open file on Azure Files SMB can ghost the
  # inode (the delete returns success but the open file lives on, and
  # the next `init_db` sees the old schema). To get a deterministic
  # empty database we:
  #   1. restart the active revision so the old container releases the
  #      file handle (and starts a NEW container — which immediately
  #      reopens app.db, so we must…)
  #   2. delete app.db right after the container starts
  #   3. restart once more so the *next* container boot creates a fresh
  #      app.db via init_db (idempotent — runs CREATE TABLE IF NOT EXISTS
  #      for every table on every startup).
  # This is heavier than a single restart but is the only sequence that
  # reliably nukes the data on SMB.
  local active_rev
  active_rev=$(query az containerapp revision list \
    -n "$AZ_ACA_APP" -g "$AZ_RG" \
    --query "[?properties.active].name | [0]" -o tsv 2>/dev/null || echo "")
  if [[ -z "$active_rev" ]]; then
    warn "No active revision — wipe will only delete app.db; start the app to reseed."
  fi

  if [[ -n "$active_rev" ]]; then
    step "Restart #1 — release the old container's file handle"
    run az containerapp revision restart \
      -n "$AZ_ACA_APP" -g "$AZ_RG" --revision "$active_rev" >/dev/null \
      || warn "Revision restart failed (continuing)."
    info "Waiting 10s for the old replica to exit"
    sleep 10
  fi

  step "Deleting app.db from share ${AZ_FILE_SHARE}"
  if az storage file show \
       --account-name "$AZ_STORAGE" --account-key "$key" \
       --share-name "$AZ_FILE_SHARE" --path app.db >/dev/null 2>&1; then
    run az storage file delete \
      --account-name "$AZ_STORAGE" --account-key "$key" \
      --share-name "$AZ_FILE_SHARE" --path app.db >/dev/null
    ok "Deleted app.db"
  else
    info "app.db not present on share — nothing to delete"
  fi

  if [[ -n "$active_rev" ]]; then
    step "Restart #2 — new container runs init_db against an empty data dir"
    run az containerapp revision restart \
      -n "$AZ_ACA_APP" -g "$AZ_RG" --revision "$active_rev" >/dev/null \
      || warn "Revision restart failed — manually restart to reseed the DB."
    info "Waiting 15s for the new replica to come up + run init_db"
    sleep 15
  fi

  step "Verifying app.db exists with a fresh schema"
  local size=""
  if az storage file show \
       --account-name "$AZ_STORAGE" --account-key "$key" \
       --share-name "$AZ_FILE_SHARE" --path app.db >/dev/null 2>&1; then
    size=$(az storage file show \
      --account-name "$AZ_STORAGE" --account-key "$key" \
      --share-name "$AZ_FILE_SHARE" --path app.db \
      --query "properties.contentLength" -o tsv 2>/dev/null || echo "?")
  fi
  if [[ -n "$size" && "$size" != "0" && "$size" != "?" ]]; then
    ok "app.db reseeded — size ${size} bytes"
  else
    warn "app.db not visible yet — give it another minute, then check status."
    warn "If a Save still 500s after that, hit /healthz to confirm the app is up."
  fi
  ok "Wipe complete."
}

# Split a hostname like "composer.smartdrivingcar.com" into apex + leading
# subdomain label(s). Apex domains are rejected: GoDaddy ALIAS records
# for ACA are awkward, so we require a subdomain like "composer.foo.com".
_split_host() {
  local host="$1"
  local n
  n=$(awk -F. '{print NF}' <<< "$host")
  if (( n < 3 )); then
    err "Custom hostname '${host}' looks like an apex (or invalid). Use a subdomain."
    return 1
  fi
  SPLIT_APEX=$(awk -F. '{print $(NF-1)"."$NF}' <<< "$host")
  SPLIT_SUB="${host%.$SPLIT_APEX}"
}

do_domain() {
  load_env
  banner
  preflight
  heading "Custom domain"

  if [[ -z "${AZ_CUSTOM_DOMAIN:-}" ]]; then
    prompt_with_default AZ_CUSTOM_DOMAIN "Custom hostname" ""
    if [[ -z "$AZ_CUSTOM_DOMAIN" ]]; then
      err "AZ_CUSTOM_DOMAIN is required."
      exit 1
    fi
    write_env_file
  fi
  local host="$AZ_CUSTOM_DOMAIN"
  _split_host "$host" || exit 1
  local sub="$SPLIT_SUB" apex="$SPLIT_APEX"

  local fqdn verif
  fqdn=$(query az containerapp show -n "$AZ_ACA_APP" -g "$AZ_RG" --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || echo "")
  verif=$(query az containerapp show -n "$AZ_ACA_APP" -g "$AZ_RG" --query properties.customDomainVerificationId -o tsv 2>/dev/null || echo "")
  if [[ -z "$fqdn" || -z "$verif" ]]; then
    err "Container App ${AZ_ACA_APP} not provisioned yet — run \`scripts/deploy.sh setup\` first."
    exit 1
  fi

  step "DNS records required at your registrar (apex: ${apex})"
  info "CNAME  Name=${sub}             Value=${fqdn}"
  info "TXT    Name=asuid.${sub}       Value=${verif}"
  pause_for_manual "Add the two DNS records, then continue" \
    "CNAME : Host=${sub}        Points to=${fqdn}      TTL=600" \
    "TXT   : Host=asuid.${sub}  Value=${verif}          TTL=600" \
    "Wait 1-3 min for propagation. Verify with:" \
    "  dig +short CNAME ${host}" \
    "  dig +short TXT   asuid.${host}"

  step "Add hostname ${host} to Container App"
  local exists
  exists=$(az containerapp hostname list -n "$AZ_ACA_APP" -g "$AZ_RG" --query "[?name=='${host}'].name" -o tsv 2>/dev/null || echo "")
  if [[ -n "$exists" ]]; then
    ok "Hostname already attached"
  else
    run az containerapp hostname add -n "$AZ_ACA_APP" -g "$AZ_RG" --hostname "$host" >/dev/null
    ok "Hostname attached"
  fi

  step "Provision + bind managed certificate (free, auto-renewing)"
  local bind_type
  bind_type=$(az containerapp hostname list -n "$AZ_ACA_APP" -g "$AZ_RG" --query "[?name=='${host}'].bindingType" -o tsv 2>/dev/null || echo "")
  if [[ "$bind_type" == "SniEnabled" ]]; then
    ok "Managed cert already bound"
  else
    run az containerapp hostname bind \
      -n "$AZ_ACA_APP" -g "$AZ_RG" \
      --hostname "$host" \
      --environment "$AZ_ACA_ENV" \
      --validation-method CNAME >/dev/null
    ok "Managed certificate bound (initial issuance can take 1-3 min)"
  fi

  step "Ensure Entra app accepts callback for https://${host}"
  local app_id
  app_id=$(query az ad app list --display-name "$AZ_ENTRA_APP" --query "[0].appId" -o tsv 2>/dev/null || echo "")
  if [[ -n "$app_id" && "$app_id" != "DRYRUN-VALUE" ]] && (( ! DRY_RUN )); then
    local want="https://${host}/.auth/login/aad/callback"
    local existing
    existing=$(az ad app show --id "$app_id" --query "web.redirectUris" -o tsv 2>/dev/null || echo "")
    if print -- "$existing" | grep -Fxq "$want"; then
      ok "Redirect URI already registered: ${want}"
    else
      local merged
      merged=$(printf "%s\n%s\n" "$existing" "$want" | awk 'NF && !seen[$0]++' | tr '\n' ' ')
      run az ad app update --id "$app_id" --web-redirect-uris ${=merged} >/dev/null
      ok "Redirect URI registered: ${want}"
    fi
  else
    warn "Entra app ${AZ_ENTRA_APP} not found — skipped redirect-URI update"
  fi

  heading "Done"
  ok "Custom domain live: https://${host}"
  info "If the cert is still provisioning, the URL may briefly serve a TLS warning — refresh after 1-3 min."
}

usage() {
  cat <<EOF
Usage: scripts/deploy.sh <command> [options]

Commands:
  setup       Interactive end-to-end provision (idempotent — safe to rerun)
  update      Rebuild image and refresh the Container App (asks before rolling)
  domain      Bind the configured AZ_CUSTOM_DOMAIN (DNS pause + managed cert)
  status      Show current state of every Azure resource
  wipe-data   DELETE app.db from the Azure Files share. Destructive; required
              after non-forward-compatible schema changes when there is no
              production data to migrate. Prompts for typed share-name confirm.
  teardown    Remove the resource group, Entra apps, and CI secrets
  help        This message

Options:
  -y, --yes        Assume yes / use defaults at every prompt
  --dry-run        Print az/gh commands without executing
  --subscription S Override Azure subscription for this invocation
EOF
}

# ───── argv ────────────────────────────────────────────────────────────
[[ $# -eq 0 ]] && { usage; exit 0; }

ACTION="$1"; shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --subscription) SUBSCRIPTION="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "Unknown option: $1"; usage; exit 2 ;;
  esac
  shift
done

[[ -n "$SUBSCRIPTION" ]] && AZ_SUBSCRIPTION="$SUBSCRIPTION"

case "$ACTION" in
  setup)     do_setup ;;
  update)    do_update ;;
  domain)    do_domain ;;
  status)    status ;;
  wipe-data) do_wipe_data ;;
  teardown)  teardown ;;
  help|-h|--help) usage ;;
  *) err "Unknown command: $ACTION"; usage; exit 2 ;;
esac
