#!/bin/bash
# Remove every git object that is not an ancestor of BASE_COMMIT, then prove it.
#
# Usage:
#   seal_gold_history.sh <phase> <base_commit> <known_fix_commit|-> <repo> [repo ...]
#
#   phase             label written into the evidence ("testbed" / "workspace")
#   base_commit       the SWE-bench base commit; it and its ancestors are kept
#   known_fix_commit  upstream commit that contains the gold modification, or "-"
#
# Emits one `SEAL_EVIDENCE {json}` line per repository and a final
# `SEAL_OK` / `SEAL_BROKEN` line. Exits non-zero unless every probe passed.
#
# This script never touches the host-side dataset; it only shrinks what the
# agent's container can reach through git.

set -u

PHASE="${1:?phase required}"
BASE_COMMIT="${2:?base_commit required}"
KNOWN_FIX="${3:?known_fix_commit required (use - for none)}"
shift 3

FAIL=0

for repo in "$@"; do
  ok=true
  reasons=""

  if [ ! -d "$repo/.git" ]; then
    echo "SEAL_EVIDENCE {\"repo\":\"$repo\",\"phase\":\"$PHASE\",\"ok\":false,\"reasons\":\"not-a-git-repo\"}"
    FAIL=1
    continue
  fi

  cd "$repo" || { FAIL=1; continue; }
  git_dir=$(git rev-parse --absolute-git-dir 2>/dev/null || echo "$repo/.git")

  # --- who had access before we changed anything ---
  pre_all_count=$(git rev-list --all --count 2>/dev/null || echo -1)
  submodules=$(
    git submodule status 2>/dev/null | grep -c '^ ' || true
  )
  known_before=false
  if [ "$KNOWN_FIX" != "-" ]; then
    if git cat-file -e "${KNOWN_FIX}^{commit}" 2>/dev/null; then
      known_before=true
    fi
  fi

  # --- seal ---
  if ! git checkout -B sealed "$BASE_COMMIT" >/dev/null 2>&1; then
    echo "SEAL_EVIDENCE {\"repo\":\"$repo\",\"phase\":\"$PHASE\",\"ok\":false,\"reasons\":\"checkout-failed\"}"
    FAIL=1
    continue
  fi
  for r in $(git remote); do git remote remove "$r" >/dev/null 2>&1 || true; done
  git for-each-ref \
      --format='%(refname)' \
      refs/heads refs/remotes refs/tags refs/replace refs/stash 2>/dev/null \
    | grep -v '^refs/heads/sealed$' \
    | while read -r ref; do git update-ref -d "$ref" >/dev/null 2>&1 || true; done
  rm -rf "$git_dir/refs/replace" "$git_dir/refs/stash" "$git_dir/logs"
  git stash clear >/dev/null 2>&1 || true
  git reflog expire --expire=now --expire-unreachable=now --all >/dev/null 2>&1 || true
  rm -f "$git_dir/objects/info/alternates"
  git worktree prune >/dev/null 2>&1 || true
  git gc --prune=now --quiet >/dev/null 2>&1 || true

  # --- probes (fail closed: every one must hold) ---
  head_now=$(git rev-parse HEAD 2>/dev/null || echo none)
  head_is_base=false
  [ "$head_now" = "$BASE_COMMIT" ] || { ok=false; reasons="$reasons head!=base"; }
  [ "$head_now" = "$BASE_COMMIT" ] && head_is_base=true

  refs=$(git for-each-ref --format='%(refname)' 2>/dev/null | paste -sd, -)
  [ "$refs" = "refs/heads/sealed" ] || { ok=false; reasons="$reasons refs=$refs"; }

  remotes=$(git remote 2>/dev/null | paste -sd, -)
  [ -z "$remotes" ] || { ok=false; reasons="$reasons remotes=$remotes"; }

  reflog_lines=$(git reflog show --all 2>/dev/null | wc -l | tr -d ' ')
  [ "$reflog_lines" = "0" ] || { ok=false; reasons="$reasons reflog=$reflog_lines"; }

  stash_lines=$(git stash list 2>/dev/null | wc -l | tr -d ' ')
  [ "$stash_lines" = "0" ] || { ok=false; reasons="$reasons stash=$stash_lines"; }

  replace_refs=$(git replace -l 2>/dev/null | paste -sd, -)
  [ -z "$replace_refs" ] || { ok=false; reasons="$reasons replace=$replace_refs"; }

  alternates=false
  [ -e "$git_dir/objects/info/alternates" ] && { alternates=true; ok=false; reasons="$reasons alternates"; }

  shallow=false
  [ -e "$git_dir/shallow" ] && shallow=true

  worktrees=$(git worktree list 2>/dev/null | wc -l | tr -d ' ')
  [ "$worktrees" = "1" ] || { ok=false; reasons="$reasons worktrees=$worktrees"; }

  [ "$submodules" = "0" ] || { ok=false; reasons="$reasons initialized_submodules=$submodules"; }

  all_count=$(git rev-list --all --count 2>/dev/null || echo -1)
  head_count=$(git rev-list HEAD --count 2>/dev/null || echo -1)
  [ "$all_count" = "$head_count" ] || { ok=false; reasons="$reasons reachable=$all_count/$head_count"; }

  fsck_out=$(git fsck --no-reflogs --unreachable --no-progress 2>/dev/null || true)
  unreachable=$(printf '%s\n' "$fsck_out" | grep -c 'unreachable commit' || true)
  [ "$unreachable" = "0" ] || { ok=false; reasons="$reasons unreachable_commits=$unreachable"; }
  # A leftover unreachable blob or tree can still hold the fix, so checking
  # commits alone is not enough.
  unreachable_objects=$(printf '%s\n' "$fsck_out" | grep -cE 'unreachable |dangling ' || true)
  [ "$unreachable_objects" = "0" ] || { ok=false; reasons="$reasons unreachable_objects=$unreachable_objects"; }

  diff_base_ok=false
  if git diff --stat "$BASE_COMMIT" >/dev/null 2>&1; then diff_base_ok=true; else ok=false; reasons="$reasons diff-base-failed"; fi

  known_after=false
  known_probe="not_requested"
  if [ "$KNOWN_FIX" != "-" ]; then
    if git cat-file -e "${KNOWN_FIX}^{commit}" 2>/dev/null; then
      known_after=true
    fi
    if [ "$known_after" = "true" ]; then
      # A commit carrying gold content is still reachable: the seal failed.
      known_probe="still_present"
      ok=false; reasons="$reasons known_fix_still_present"
    elif [ "$known_before" = "true" ]; then
      known_probe="confirmed_removed"
    else
      # A configured sha this image never had proves nothing, so it is not a
      # pass. Fix the mapping in known_fix_commits.json and re-run.
      known_probe="unverified_absent_before"
      ok=false; reasons="$reasons known_fix_absent_before_unverified"
    fi
  fi

  if command -v jq >/dev/null 2>&1; then
    report=$(jq -cn \
      --arg repo "$repo" --arg phase "$PHASE" --arg base "$BASE_COMMIT" \
      --arg head "$head_now" --arg refs "$refs" --arg remotes "$remotes" \
      --arg replace "$replace_refs" --arg known_fix "$KNOWN_FIX" --arg reasons "$reasons" \
      --argjson pre_all "$pre_all_count" --argjson head_is_base "$head_is_base" \
      --argjson reflog "$reflog_lines" --argjson stash "$stash_lines" \
      --argjson alternates "$alternates" --argjson shallow "$shallow" \
      --argjson worktrees "$worktrees" --argjson submodules "$submodules" \
      --argjson all_count "$all_count" --argjson head_count "$head_count" \
      --argjson unreachable "$unreachable" --argjson diff_base_ok "$diff_base_ok" \
      --argjson unreachable_objects "$unreachable_objects" \
      --argjson known_before "$known_before" --argjson known_after "$known_after" \
      --arg known_probe "$known_probe" \
      --argjson ok "$ok" \
      '{repo:$repo,phase:$phase,base_commit:$base,head:$head,head_is_base:$head_is_base,
        refs:$refs,remotes:$remotes,replace_refs:$replace,reflog_lines:$reflog,
        stash_lines:$stash,alternates:$alternates,shallow:$shallow,worktrees:$worktrees,
        initialized_submodules:$submodules,pre_all_count:$pre_all,all_count:$all_count,
        head_count:$head_count,unreachable_commits:$unreachable,
        unreachable_objects:$unreachable_objects,diff_base_ok:$diff_base_ok,
        known_fix_commit:$known_fix,known_fix_present_before:$known_before,
        known_fix_present_after:$known_after,known_probe:$known_probe,
        ok:$ok,reasons:$reasons}')
  else
    # Fallback for hosts without jq; field values here never contain quotes.
    report="{\"repo\":\"$repo\",\"phase\":\"$PHASE\",\"base_commit\":\"$BASE_COMMIT\",\"head\":\"$head_now\",\"head_is_base\":$head_is_base,\"refs\":\"$refs\",\"remotes\":\"$remotes\",\"replace_refs\":\"$replace_refs\",\"reflog_lines\":$reflog_lines,\"stash_lines\":$stash_lines,\"alternates\":$alternates,\"shallow\":$shallow,\"worktrees\":$worktrees,\"initialized_submodules\":$submodules,\"pre_all_count\":$pre_all_count,\"all_count\":$all_count,\"head_count\":$head_count,\"unreachable_commits\":$unreachable,\"unreachable_objects\":$unreachable_objects,\"diff_base_ok\":$diff_base_ok,\"known_fix_commit\":\"$KNOWN_FIX\",\"known_fix_present_before\":$known_before,\"known_fix_present_after\":$known_after,\"known_probe\":\"$known_probe\",\"ok\":$ok,\"reasons\":\"$reasons\"}"
  fi
  echo "SEAL_EVIDENCE $report"

  [ "$ok" = "true" ] || FAIL=1
done

if [ "$FAIL" = "0" ]; then
  echo SEAL_OK
else
  echo SEAL_BROKEN
fi
exit "$FAIL"
