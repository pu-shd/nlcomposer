#!/usr/bin/env bash
# Roll one deployment (a config repo directory) onto its Azure Container App.
#
# Called by a config repo's deploy workflow after it has checked out this
# engine at the commit pinned in template.lock, and by `deploy.sh update`.
#
#   scripts/ci-deploy.sh --config <dir> --engine-ref <vX.Y.Z> [--check-only] [--dry-run]
#
# <dir> holds:
#   deploy.env     KEY=VALUE deployment targets (never sourced; only the keys
#                  below are read, and each value is validated)
#   instance.json  per-deployment brand config (see app/instance.py); passed
#                  to the app as NLC_INSTANCE_JSON
#
# Steps: validate config → build image <app>:<ref>-<sha7> in the unit's ACR
# (reused if that tag exists) → roll a new revision → wait until it is
# Running+Healthy and is the latest ready revision → HTTPS smoke test on the
# custom domain (Easy Auth's 401/302 counts as up).
#
# Environment:
#   ENGINE_ROOT       engine checkout (default: this script's repo)
#   ENGINE_SHA        engine commit (default: git rev-parse HEAD in ENGINE_ROOT)
#   NLC_PYTHON        python with the engine's deps for the instance check;
#                     unset = run the check in a container built from ENGINE_ROOT
#   NLC_POLL_SECONDS  revision health poll interval (default 10)
#   NLC_POLL_MAX      max polls before giving up (default 60)
set -euo pipefail

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

die() { echo "::error::$*" >&2; exit 1; }
say() { echo "▸ $*"; }

CONFIG_DIR=""
ENGINE_REF=""
CHECK_ONLY=0
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG_DIR="${2:-}"; shift ;;
    --engine-ref) ENGINE_REF="${2:-}"; shift ;;
    --check-only) CHECK_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
  shift
done

[ -n "$CONFIG_DIR" ] || { echo "--config is required" >&2; usage 2; }
[ -n "$ENGINE_REF" ] || { echo "--engine-ref is required" >&2; usage 2; }
[[ "$ENGINE_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$ ]] || die "bad --engine-ref '$ENGINE_REF'"
[ -d "$CONFIG_DIR" ] || die "config directory '$CONFIG_DIR' not found"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE_ROOT="${ENGINE_ROOT:-$(dirname "$SCRIPT_DIR")}"
POLL_SECONDS="${NLC_POLL_SECONDS:-10}"
POLL_MAX="${NLC_POLL_MAX:-60}"

# ── deploy.env: read an allowlist of keys, never `source` it ───────────────
DEPLOY_ENV="$CONFIG_DIR/deploy.env"
[ -f "$DEPLOY_ENV" ] || die "$DEPLOY_ENV not found"
AZ_RG="" AZ_ACR="" AZ_ACA_APP="" AZ_CUSTOM_DOMAIN=""
lineno=0
while IFS= read -r line || [ -n "$line" ]; do
  lineno=$((lineno + 1))
  case "$line" in ''|'#'*) continue ;; esac
  [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]] || die "$DEPLOY_ENV:$lineno: not KEY=VALUE"
  key="${BASH_REMATCH[1]}"
  val="${BASH_REMATCH[2]}"
  if [[ "$val" =~ ^\"(.*)\"$ ]]; then val="${BASH_REMATCH[1]}"; fi
  case "$key" in
    AZ_RG|AZ_ACR|AZ_ACA_APP|AZ_CUSTOM_DOMAIN)
      [[ -z "$val" || "$val" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
        || die "$DEPLOY_ENV:$lineno: $key has characters outside [A-Za-z0-9._-]"
      printf -v "$key" '%s' "$val"
      ;;
  esac
done < "$DEPLOY_ENV"
for required in AZ_RG AZ_ACR AZ_ACA_APP; do
  [ -n "${!required}" ] || die "$DEPLOY_ENV: $required is required"
done

# ── instance.json: validated by the engine itself ──────────────────────────
INSTANCE="$CONFIG_DIR/instance.json"
[ -f "$INSTANCE" ] || die "$INSTANCE not found (use {} for engine defaults)"
INSTANCE_ABS="$(cd "$(dirname "$INSTANCE")" && pwd)/$(basename "$INSTANCE")"
if [ -n "${NLC_PYTHON:-}" ]; then
  (cd "$ENGINE_ROOT" && "$NLC_PYTHON" -m app.instance check "$INSTANCE_ABS") \
    || die "$INSTANCE failed validation"
else
  command -v docker >/dev/null || die "docker (or NLC_PYTHON) is required to validate $INSTANCE"
  docker build -q -t nlcomposer-instance-check "$ENGINE_ROOT" >/dev/null
  docker run --rm --network none -v "$INSTANCE_ABS:/cfg/instance.json:ro" \
    nlcomposer-instance-check python -m app.instance check /cfg/instance.json \
    || die "$INSTANCE failed validation"
fi
INSTANCE_JSON="$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), separators=(",", ":")))' "$INSTANCE")"

ENGINE_SHA="${ENGINE_SHA:-$(git -C "$ENGINE_ROOT" rev-parse HEAD)}"
[[ "$ENGINE_SHA" =~ ^[0-9a-f]{40}$ ]] || die "ENGINE_SHA '$ENGINE_SHA' is not a full commit sha"
TAG="${ENGINE_REF}-${ENGINE_SHA:0:7}"
IMAGE="${AZ_ACR}.azurecr.io/${AZ_ACA_APP}:${TAG}"
REF_SLUG="$(printf '%s' "$ENGINE_REF" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9\n' '-')"
SUFFIX="${REF_SLUG}-r${GITHUB_RUN_NUMBER:-$(date -u +%H%M%S)}"
SUFFIX="${SUFFIX:0:30}"
REVISION="${AZ_ACA_APP}--${SUFFIX}"

say "config      $CONFIG_DIR (app $AZ_ACA_APP in $AZ_RG)"
say "engine      $ENGINE_REF @ ${ENGINE_SHA:0:7}"
say "image       $IMAGE"
say "revision    $REVISION"

if [ "$CHECK_ONLY" = 1 ]; then
  say "check-only: configuration is valid"
  exit 0
fi
if [ "$DRY_RUN" = 1 ]; then
  say "dry-run: would build $IMAGE if absent, roll $REVISION with NLC_INSTANCE_JSON (${#INSTANCE_JSON} bytes), wait for health${AZ_CUSTOM_DOMAIN:+, smoke https://$AZ_CUSTOM_DOMAIN/}"
  exit 0
fi

# ── image ──────────────────────────────────────────────────────────────────
have_tag="$(az acr repository show-tags -n "$AZ_ACR" --repository "$AZ_ACA_APP" \
  --query "length([?@=='$TAG'])" -o tsv 2>/dev/null || echo 0)"
if [ "$have_tag" = "1" ]; then
  say "image exists, reusing"
else
  say "building image in $AZ_ACR"
  az acr build --registry "$AZ_ACR" --image "$AZ_ACA_APP:$TAG" \
    --file "$ENGINE_ROOT/Dockerfile" "$ENGINE_ROOT" --output none
fi

# ── revision ───────────────────────────────────────────────────────────────
say "rolling new revision"
az containerapp update -n "$AZ_ACA_APP" -g "$AZ_RG" \
  --image "$IMAGE" \
  --revision-suffix "$SUFFIX" \
  --set-env-vars "NLC_INSTANCE_JSON=$INSTANCE_JSON" \
  --output none

state=""
for _ in $(seq 1 "$POLL_MAX"); do
  state="$(az containerapp revision show -n "$AZ_ACA_APP" -g "$AZ_RG" --revision "$REVISION" \
    --query "[properties.runningState, properties.healthState]" -o tsv 2>/dev/null | tr '\t\n' '  ' || true)"
  case "$state" in
    Running*Healthy*|RunningAtMaxScale*Healthy*) break ;;
    *Failed*|*Unhealthy*|*Degraded*)
      die "revision $REVISION is '$state'; the previous revision keeps serving. Logs: az containerapp logs show -n $AZ_ACA_APP -g $AZ_RG --revision $REVISION" ;;
  esac
  sleep "$POLL_SECONDS"
done
case "$state" in
  Running*Healthy*|RunningAtMaxScale*Healthy*) say "revision healthy ($state)" ;;
  *) die "revision $REVISION not healthy after $((POLL_SECONDS * POLL_MAX))s (last state '$state')" ;;
esac

latest="$(az containerapp show -n "$AZ_ACA_APP" -g "$AZ_RG" \
  --query properties.latestReadyRevisionName -o tsv)"
[ "$latest" = "$REVISION" ] || die "latest ready revision is '$latest', expected '$REVISION'"

# ── smoke ──────────────────────────────────────────────────────────────────
if [ -n "$AZ_CUSTOM_DOMAIN" ]; then
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "https://$AZ_CUSTOM_DOMAIN/" || echo 000)"
  case "$code" in
    200|302|401) say "https://$AZ_CUSTOM_DOMAIN/ answered $code" ;;
    *) die "https://$AZ_CUSTOM_DOMAIN/ answered $code (expected 200, 302 or 401)" ;;
  esac
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Deployed \`$CONFIG_DIR\`"
    echo "| | |"
    echo "|---|---|"
    echo "| engine | \`$ENGINE_REF\` (\`${ENGINE_SHA:0:7}\`) |"
    echo "| image | \`$IMAGE\` |"
    echo "| revision | \`$REVISION\` ($state) |"
    if [ -n "$AZ_CUSTOM_DOMAIN" ]; then
      echo "| smoke | https://$AZ_CUSTOM_DOMAIN/ → $code |"
    fi
  } >> "$GITHUB_STEP_SUMMARY"
fi
say "done"
