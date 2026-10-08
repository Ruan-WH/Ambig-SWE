#!/bin/bash
# Verify every known-fix mapping against the real instance image, without running
# an agent. Each image is used once in a throwaway container, so this costs
# seconds instead of a full episode and catches a wrong sha before the sealed arm
# aborts mid-run.
#
# Usage:
#   verify_fix_mappings.sh <dataset-json> [image-prefix]
#
#   dataset-json   provides base_commit, e.g. <run dir>/hidden-dataset-20.json
#   image-prefix   default "xingyaoww/"; images that are not local are skipped
#
# Exit code: 0 when nothing failed, 1 when at least one mapping failed.

set -u

DATASET="${1:?usage: verify_fix_mappings.sh <dataset-json> [image-prefix]}"
PREFIX="${2:-xingyaoww/}"

ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
SEAL="$ROOT/evaluation/benchmarks/swe_bench/scripts/setup/seal_gold_history.sh"
MAP="$ROOT/evaluation/benchmarks/swe_bench/data/known_fix_commits.json"

for f in "$SEAL" "$MAP" "$DATASET"; do
  [ -f "$f" ] || { echo "missing file: $f" >&2; exit 2; }
done

TMPLIST="$(mktemp)"
trap 'rm -f "$TMPLIST"' EXIT

python3 - "$DATASET" "$MAP" <<'PY' > "$TMPLIST"
import json
import sys

with open(sys.argv[1]) as f:
    dataset = json.load(f)
with open(sys.argv[2]) as f:
    commits = json.load(f).get('commits', {})

for row in dataset:
    instance_id = row['instance_id']
    if instance_id in commits:
        print(instance_id, row['base_commit'], commits[instance_id])
PY

pass_count=0
fail_count=0
skip_count=0

while read -r instance_id base_commit fix_commit; do
  [ -n "$instance_id" ] || continue
  image="$(printf '%s' "${PREFIX}sweb.eval.x86_64.${instance_id//__/_s_}:latest" | tr 'A-Z' 'a-z')"
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    printf '%-28s SKIP   image not local (%s)\n' "$instance_id" "$image"
    skip_count=$((skip_count + 1))
    continue
  fi
  output="$(
    docker run --rm --entrypoint bash \
      -v "$SEAL":/seal_gold_history.sh:ro \
      "$image" -c \
      "bash /seal_gold_history.sh testbed $base_commit $fix_commit /testbed" 2>&1
  )"
  probe="$(printf '%s' "$output" | grep -o '"known_probe":"[a-z_]*"' | head -1)"
  if printf '%s' "$output" | grep -q 'SEAL_OK' \
    && printf '%s' "$probe" | grep -q 'confirmed_removed'; then
    printf '%-28s OK     %s\n' "$instance_id" "$probe"
    pass_count=$((pass_count + 1))
  else
    printf '%-28s FAIL   %s\n' "$instance_id" "${probe:-no-evidence}"
    fail_count=$((fail_count + 1))
  fi
done < "$TMPLIST"

echo
echo "verified: $pass_count   failed: $fail_count   skipped: $skip_count"
[ "$fail_count" -eq 0 ]
