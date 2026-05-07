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
ENV_FILE="${REPO_ROOT}/.deploy.env"
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
GH_REPO="${GH_REPO}"
EOF
  ok "Saved configuration to ${ENV_FILE}"
}

prompt_with_default() {
  local var="$1" prompt="$2" default="$3" current="${(P)var:-}"
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
    run az containerapp update \
      -n "$AZ_ACA_APP" -g "$AZ_RG" \
      --image "$image" >/dev/null
    ok "Updated"
  else
    run az containerapp create \
      -n "$AZ_ACA_APP" -g "$AZ_RG" \
      --environment "$AZ_ACA_ENV" \
      --image "$image" \
      --target-port 8000 --ingress external \
      --registry-server "${AZ_ACR}.azurecr.io" \
      --registry-identity system \
      --system-assigned \
      --env-vars "SDC_DATA_DIR=/app/data" \
      --min-replicas 1 --max-replicas 1 \
      --cpu 0.5 --memory 1.0Gi >/dev/null
    ok "Created"
  fi

  step "Bind file-share volume to /app/data"
  if (( DRY_RUN )); then
    info "(dry-run) would export YAML, inject sdc-data-mount volume + mount, re-apply"
  else
    # ACA's "yaml-bound" volume binding has no convenient CLI patch; export, edit, re-apply.
    local tmpyaml; tmpyaml=$(mktemp -t aca-XXXX.yaml)
    az containerapp show -n "$AZ_ACA_APP" -g "$AZ_RG" -o yaml > "$tmpyaml"
    if grep -q "name: sdc-data-mount" "$tmpyaml" 2>/dev/null; then
      ok "Volume already bound"
    else
      python3 - "$tmpyaml" <<'PY'
import sys, yaml, pathlib
p = pathlib.Path(sys.argv[1])
doc = yaml.safe_load(p.read_text())
tmpl = doc.setdefault("properties", {}).setdefault("template", {})
containers = tmpl.setdefault("containers", [])
if containers:
    mounts = containers[0].setdefault("volumeMounts", [])
    if not any(m.get("volumeName") == "sdc-data-mount" for m in mounts):
        mounts.append({"volumeName": "sdc-data-mount", "mountPath": "/app/data"})
volumes = tmpl.setdefault("volumes", [])
if not any(v.get("name") == "sdc-data-mount" for v in volumes):
    volumes.append({
        "name": "sdc-data-mount",
        "storageType": "AzureFile",
        "storageName": "sdc-data",
    })
p.write_text(yaml.safe_dump(doc, sort_keys=False))
PY
      run az containerapp update -n "$AZ_ACA_APP" -g "$AZ_RG" --yaml "$tmpyaml" >/dev/null
      ok "Volume bound"
    fi
    rm -f "$tmpyaml"
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

  pause_for_manual "Generate Entra app client secret" \
    "Open https://portal.azure.com → Entra ID → App registrations → ${AZ_ENTRA_APP}" \
    "Certificates & secrets → New client secret (1-year recommended)" \
    "Copy the secret VALUE — you will paste it next."

  if (( ASSUME_YES )); then
    info "(auto-yes) skipping client-secret bind"
  else
    print -nP "${C_CYAN}Paste client-secret value (will be stored only on the Container App):${C_RESET} "
    local client_secret
    read -rs client_secret || client_secret=""
    print
    if [[ -n "$client_secret" ]]; then
      step "Bind Entra auth to Container App"
      run az containerapp auth microsoft update \
        -n "$AZ_ACA_APP" -g "$AZ_RG" \
        --client-id "$ENTRA_APP_ID" \
        --client-secret "$client_secret" \
        --tenant-id "$(az account show --query tenantId -o tsv)" >/dev/null
      run az containerapp auth update \
        -n "$AZ_ACA_APP" -g "$AZ_RG" \
        --enabled true \
        --action RedirectToLoginPage \
        --redirect-provider azureactivedirectory >/dev/null || true
      ok "Auth enabled (redirect-to-login)"
    else
      warn "No secret provided — auth left in current state."
    fi
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
  build_and_push_image
  ensure_container_app
  ok "Update complete."
}

usage() {
  cat <<EOF
Usage: scripts/deploy.sh <command> [options]

Commands:
  setup       Interactive end-to-end provision (idempotent — safe to rerun)
  update      Rebuild image and refresh the Container App
  status      Show current state of every Azure resource
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
  setup)    do_setup ;;
  update)   do_update ;;
  status)   status ;;
  teardown) teardown ;;
  help|-h|--help) usage ;;
  *) err "Unknown command: $ACTION"; usage; exit 2 ;;
esac
