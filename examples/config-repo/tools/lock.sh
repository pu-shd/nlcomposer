#!/usr/bin/env bash
# Verify template.lock against the engine's published tag and print ref=/sha=.
# Appends the same lines to $GITHUB_OUTPUT when it is set.
#
#   tools/lock.sh [template.lock]
#
# Fails unless: repo is the engine, ref is vX.Y.Z, sha is a full commit, and
# the tag `ref` in that repo resolves to exactly `sha` (a moved or forged tag
# cannot slip a different commit into a deploy).
set -euo pipefail
LOCK="${1:-template.lock}"
ENGINE_REPO="pu-shd/nlcomposer"
die() { echo "::error::$*" >&2; exit 1; }

[ -f "$LOCK" ] || die "$LOCK not found"
read -r repo ref sha < <(python3 - "$LOCK" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("repo", ""), d.get("ref", ""), d.get("sha", ""))
PY
) || die "$LOCK is not valid JSON"

[ "$repo" = "$ENGINE_REPO" ] || die "$LOCK repo '$repo' is not $ENGINE_REPO"
[[ "$ref" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "$LOCK ref '$ref' is not vX.Y.Z"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die "$LOCK sha '$sha' is not a full commit sha"

remote="$(git ls-remote "https://github.com/${repo}.git" "refs/tags/${ref}" "refs/tags/${ref}^{}")" \
  || die "cannot list tags of $repo"
peeled="$(awk -v r="refs/tags/${ref}^{}" '$2 == r { print $1 }' <<<"$remote")"
plain="$(awk -v r="refs/tags/${ref}" '$2 == r { print $1 }' <<<"$remote")"
tag_sha="${peeled:-$plain}"
[ -n "$tag_sha" ] || die "tag $ref not found in $repo"
[ "$tag_sha" = "$sha" ] || die "$LOCK pins $sha but $repo tag $ref is $tag_sha"

echo "ref=$ref"
echo "sha=$sha"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  { echo "ref=$ref"; echo "sha=$sha"; } >> "$GITHUB_OUTPUT"
fi
