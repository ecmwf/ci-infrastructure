#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

# Apply the PR rules to develop, main, master and the default branch: no deletion or force-push,
# one approval (from a code owner where CODEOWNERS covers a changed file), and the required CI checks;
# creates the downstream-CI labels where missing.
# Needs admin on each repo. Re-runnable: rulesets are updated by name.
#
#   scripts/apply-pr-rules.sh [--dry-run] [--bot-app-id <id>] [--delete-existing] ecmwf/eckit ecmwf/fdb ...
#
# --bot-app-id: a GitHub App whose own PRs need no review (e.g. the workflow
# regenerator, so it can auto-merge). CI still has to pass.
# --delete-existing: also delete the repo's other branch rulesets and the classic
# protection on those branches. Tag and organization rulesets are never touched.
set -euo pipefail

dry=false
bot_app_id=""
delete_existing=false
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=true; shift ;;
    --bot-app-id) bot_app_id="${2:?--bot-app-id needs an App id}"; shift 2 ;;
    --delete-existing) delete_existing=true; shift ;;
    --*) echo "unknown option: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
[ $# -gt 0 ] || { echo "usage: $0 [--dry-run] [--bot-app-id <id>] [--delete-existing] owner/repo..." >&2; exit 2; }
[[ -z "$bot_app_id" || "$bot_app_id" =~ ^[0-9]+$ ]] || { echo "--bot-app-id must be numeric" >&2; exit 2; }

# gh with a JSON body on stdin; in a dry run, only print it.
call() {
  if $dry; then echo "  would: gh $*"; cat >/dev/null; else gh "$@" >/dev/null; fi
}

# Print the change from $2 (current) to $3 (new), both JSON, as a unified diff.
show_diff() {
  local d
  if d=$(diff -u --label "$1 (current)" --label "$1 (new)" <(jq -S . <<< "$2") <(jq -S . <<< "$3")); then
    echo "    $1: unchanged"
  else
    while IFS= read -r line; do echo "    $line"; done <<< "$d"
  fi
}
# The current ruleset, cut to the fields the new one sets: GitHub adds defaults
# to rule parameters that would otherwise show up as changes on every run.
current_ruleset() {
  gh api "repos/$repo/rulesets/$1" | jq --argjson new "$2" '{name, target, enforcement, conditions, bypass_actors,
    rules: [.rules[] | . as $r | ([$new.rules[] | select(.type == $r.type) | .parameters // {} | keys] | add) as $k |
      if $k == null or .parameters == null then . else .parameters |= with_entries(select(.key | IN($k[]))) end]}'
}

# RepositoryRole ids: 2 = maintain, 5 = admin.
roles() { jq -n --arg m "$1" '[2, 5] | map({actor_type: "RepositoryRole", actor_id: ., bypass_mode: $m})'; }
# The roles, plus the bot App. Exempt, not pull_request: auto-merge never takes a bypass, it waits for the review.
approval_bypass() {
  if [ -n "$bot_app_id" ]; then
    jq --argjson id "$bot_app_id" '. + [{actor_type: "Integration", actor_id: $id, bypass_mode: "exempt"}]' <<< "$(roles exempt)"
  else
    roles exempt
  fi
}
pull_request() {
  jq -n --argjson n "$1" --argjson owners "$2" '{type: "pull_request", parameters: {
    required_approving_review_count: $n, dismiss_stale_reviews_on_push: false,
    require_code_owner_review: $owners, require_last_push_approval: false,
    required_review_thread_resolution: false, allowed_merge_methods: ["merge", "squash", "rebase"]}}'
}
ruleset() {
  jq -n --arg name "$1" --argjson rules "$2" --argjson bypass "$3" '{name: $name, target: "branch",
    enforcement: "active", conditions: {ref_name: {include: ["~DEFAULT_BRANCH", "refs/heads/develop", "refs/heads/main", "refs/heads/master"], exclude: []}},
    rules: $rules, bypass_actors: $bypass}'
}

labels=$(cat <<'EOF'
downstream-ci-not-needed	C5DEF5	Deliberately skip the downstream fan-out
run-downstream-ci:1	CAB5CA	Run downstream CI up to level 1 (direct consumers)
run-downstream-ci:2	CE8CA0	Run downstream CI up to level 2
run-downstream-ci:3	D36375	Run downstream CI up to level 3
run-downstream-ci:all	D73A4A	Run the whole downstream fan-out for this PR
EOF
)

failed=false
for repo in "$@"; do
  echo "$repo"
  # The label gate posts each lane's downstream/<lane> status (at once for a lane without consumers),
  # which the lane's orchestrator settles; without it nothing posts them.
  if ! gh api "repos/$repo/contents/.github/workflows/pr-label-downstream-ci.yml" --silent 2>/dev/null; then
    echo "  error: no pr-label-downstream-ci.yml; merge the regenerate bot's PR first" >&2
    failed=true
    continue
  fi

  settings='{"allow_auto_merge": true, "delete_branch_on_merge": true, "allow_update_branch": true}'
  show_diff "repo settings" "$(gh api "repos/$repo" --jq '{allow_auto_merge, delete_branch_on_merge, allow_update_branch}')" "$settings"
  call api -X PATCH "repos/$repo" --input - <<< "$settings"

  checks='["ci-result", "downstream/runner", "downstream/hpc-atos"]'
  echo "  required checks: $(jq -r 'join(", ")' <<< "$checks")"

  have=$(gh label list -R "$repo" -L 1000 --json name --jq '.[].name')
  while IFS=$'\t' read -r name color description; do
    if ! grep -qxF "$name" <<< "$have"; then
      call label create "$name" -R "$repo" --color "$color" --description "$description" < /dev/null
      echo "  created label $name"
    fi
  done <<< "$labels"

  # 15368 = GitHub Actions, which posts all of them.
  status_checks=$(jq -n --argjson c "$checks" '{type: "required_status_checks", parameters: {
    strict_required_status_checks_policy: true, do_not_enforce_on_create: true,
    required_status_checks: ($c | map({context: ., integration_id: 15368}))}}')

  bodies=(
    "$(ruleset default-branch-integrity "$(jq -n --argjson p "$(pull_request 0 false)" '[{type: "deletion"}, {type: "non_fast_forward"}, $p]')" '[]')"
    "$(ruleset default-branch-approval "[$(pull_request 1 true)]" "$(approval_bypass)")"
    "$(ruleset default-branch-ci "[$status_checks]" "$(roles pull_request)")"
  )

  existing=$(gh api "repos/$repo/rulesets?includes_parents=false" --jq '[.[] | select(.source_type == "Repository" and .target == "branch") | {(.name): .id}] | add // {}')
  for body in "${bodies[@]}"; do
    name=$(jq -r .name <<< "$body")
    id=$(jq -r --arg n "$name" '.[$n] // empty' <<< "$existing")
    if [ -n "$id" ]; then
      show_diff "$name" "$(current_ruleset "$id" "$body")" "$body"
      call api -X PUT "repos/$repo/rulesets/$id" --input - <<< "$body"
      echo "  updated $name"
    else
      show_diff "$name" '{}' "$body"
      call api -X POST "repos/$repo/rulesets" --input - <<< "$body"
      echo "  created $name"
    fi
  done

  others=$(jq -r 'to_entries[] | select(.key | IN("default-branch-integrity", "default-branch-approval", "default-branch-ci") | not) | "\(.value) \(.key)"' <<< "$existing")
  if $delete_existing; then
    while read -r id name; do
      [ -n "$id" ] || continue
      show_diff "$name" "$(gh api "repos/$repo/rulesets/$id" --jq '{name, target, enforcement, conditions, rules, bypass_actors}')" '{}'
      call api -X DELETE "repos/$repo/rulesets/$id" < /dev/null
      echo "  deleted $name"
    done <<< "$others"
    default_branch=$(gh api "repos/$repo" --jq .default_branch)
    for branch in $(printf '%s\n' develop main master "$default_branch" | sort -u); do
      if protection=$(gh api "repos/$repo/branches/$branch/protection" 2>/dev/null); then
        show_diff "classic protection on $branch" "$(jq 'walk(if type == "object" then del(.url, .contexts_url, .users_url, .teams_url, .apps_url) else . end)' <<< "$protection")" '{}'
        call api -X DELETE "repos/$repo/branches/$branch/protection" < /dev/null
        echo "  deleted classic protection on $branch"
      fi
    done
  elif [ -n "$others" ]; then
    echo "  note: other rulesets left untouched: $(cut -d' ' -f2- <<< "$others" | paste -sd, - | sed 's/,/, /g')"
  fi
done
$failed && exit 1
exit 0
