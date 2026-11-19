#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

# Apply the organisation baseline to develop, main, master and the default branch of every repo:
# no deletion or force-push, changes via PR with one approval (dismissed by a new push, never from the
# last pusher), and workflow changes approved by a team.
# Nobody pushes directly; admins and maintainers may merge a PR without approval.
# Needs organisation owner. Re-runnable: rulesets are updated by name; other org rulesets are left alone.
#
#   scripts/apply-org-rules.sh [--dry-run] [--evaluate] [--admin-role <id>]... [--workflow-team <slug>] ecmwf
#
# --evaluate: create the rulesets in evaluate mode (logged in Rule insights, not enforced).
# --admin-role: a custom repository role treated as admin (e.g. GateKeeper); repeatable.
# --workflow-team: the team whose approval changes to .github/workflows and .github/actions need;
# without it, that ruleset is not created.
set -euo pipefail

dry=false
enforcement=active
admin_roles=(5)
workflow_team=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=true; shift ;;
    --evaluate) enforcement=evaluate; shift ;;
    --admin-role) admin_roles+=("${2:?--admin-role needs a role id}"); shift 2 ;;
    --workflow-team) workflow_team="${2:?--workflow-team needs a team slug}"; shift 2 ;;
    --*) echo "unknown option: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
[ $# -eq 1 ] || { echo "usage: $0 [--dry-run] [--evaluate] [--admin-role <id>]... [--workflow-team <slug>] org" >&2; exit 2; }
org=$1
for id in "${admin_roles[@]}"; do
  [[ "$id" =~ ^[0-9]+$ ]] || { echo "role ids must be numeric: $id" >&2; exit 2; }
done

call() {
  if $dry; then echo "  would: gh $*"; cat >/dev/null; else gh "$@" >/dev/null; fi
}

show_diff() {
  local d
  local sort='if .bypass_actors then .bypass_actors |= sort_by(.actor_type, .actor_id) else . end'
  if d=$(diff -u --label "$1 (current)" --label "$1 (new)" <(jq -S "$sort" <<< "$2") <(jq -S "$sort" <<< "$3")); then
    echo "    $1: unchanged"
  else
    while IFS= read -r line; do echo "    $line"; done <<< "$d"
  fi
}
# Cut to the fields the new ruleset sets, as in apply-pr-rules.sh.
current_ruleset() {
  gh api "orgs/$org/rulesets/$1" | jq --argjson new "$2" '{name, target, enforcement, conditions, bypass_actors,
    rules: [.rules[] | . as $r | ([$new.rules[] | select(.type == $r.type) | .parameters // {} | keys] | add) as $k |
      if $k == null or .parameters == null then . else .parameters |= with_entries(select(.key | IN($k[]))) end]}'
}

# Admins: organisation owners, the admin role (5) and any --admin-role; maintain is 2.
admins() {
  jq -n --arg m "$1" --argjson ids "$(printf '%s\n' "${admin_roles[@]}" | jq -s .)" '
    [{actor_type: "OrganizationAdmin", actor_id: null, bypass_mode: $m}]
    + ($ids | map({actor_type: "RepositoryRole", actor_id: ., bypass_mode: $m}))'
}
pull_request() {
  jq -n --argjson n "$1" --argjson reviewers "$2" '{type: "pull_request", parameters: ({
    required_approving_review_count: $n, dismiss_stale_reviews_on_push: true,
    require_code_owner_review: false, require_last_push_approval: true,
    required_review_thread_resolution: false, allowed_merge_methods: ["merge", "squash", "rebase"]}
    + if $reviewers == [] then {} else {required_reviewers: $reviewers} end)}'
}
ruleset() {
  jq -n --arg name "$1" --arg e "$enforcement" --argjson rules "$2" --argjson bypass "$3" '{name: $name, target: "branch",
    enforcement: $e, conditions: {repository_name: {include: ["~ALL"], exclude: []},
      ref_name: {include: ["~DEFAULT_BRANCH", "refs/heads/develop", "refs/heads/main", "refs/heads/master"], exclude: []}},
    rules: $rules, bypass_actors: $bypass}'
}

pr_bypass=$(jq -s 'add' <(admins pull_request) <(jq -n '[{actor_type: "RepositoryRole", actor_id: 2, bypass_mode: "pull_request"}]'))
bodies=(
  "$(ruleset org-branch-integrity '[{"type": "deletion"}, {"type": "non_fast_forward"}]' '[]')"
  "$(ruleset org-branch-pr "[$(pull_request 1 '[]')]" "$pr_bypass")"
)
if [ -n "$workflow_team" ]; then
  team_id=$(gh api "orgs/$org/teams/$workflow_team" --jq .id)
  reviewers=$(jq -n --argjson id "$team_id" '[{file_patterns: [".github/workflows/**", ".github/actions/**"],
    minimum_approvals: 1, reviewer: {type: "Team", id: $id}}]')
  # Admins of the repo merge a workflow PR through the bypass, but cannot push one.
  bodies+=("$(ruleset org-workflow-review "[$(pull_request 0 "$reviewers")]" "$(admins pull_request)")")
else
  echo "note: no --workflow-team, so org-workflow-review is not created or updated"
fi

echo "$org ($enforcement)"
existing=$(gh api "orgs/$org/rulesets" --paginate --jq '[.[] | select(.source_type == "Organization") | {(.name): .id}] | add // {}' | jq -s 'add // {}')
for body in "${bodies[@]}"; do
  name=$(jq -r .name <<< "$body")
  id=$(jq -r --arg n "$name" '.[$n] // empty' <<< "$existing")
  if [ -n "$id" ]; then
    show_diff "$name" "$(current_ruleset "$id" "$body")" "$body"
    call api -X PUT "orgs/$org/rulesets/$id" --input - <<< "$body"
    echo "  updated $name"
  else
    show_diff "$name" '{}' "$body"
    call api -X POST "orgs/$org/rulesets" --input - <<< "$body"
    echo "  created $name"
  fi
done
others=$(jq -r 'keys - ["org-branch-integrity", "org-branch-pr", "org-workflow-review"] | join(", ")' <<< "$existing")
[ -z "$others" ] || echo "  note: other org rulesets left untouched: $others"
