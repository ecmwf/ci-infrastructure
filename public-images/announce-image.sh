# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
#
# shellcheck shell=bash
#
# Say which image this job runs in, from inside the image and with no workflow
# step to declare. Every base sets BASH_ENV to this file, and GitHub runs each
# step as `bash --noprofile --norc -e -o pipefail`, which sources it. See
# IMAGES.md.
#
# Everything goes to stderr. This runs inside whichever step happens to be
# first, and if that step is not bash the first bash of the job can be a
# command substitution -- whose stdout is a captured value, not the log. That
# rules out `::notice`, which the runner only parses on stdout; the annotation
# stays with actions/announce-image, which runs as a step of its own.

__ci_announce_image() {
  [ -n "${CI_IMAGE_NAME:-}" ] || return 0

  # actions/announce-image owns the step it runs in: it prints the same facts
  # plus an annotation and whatever `extra` its caller passed.
  [ -n "${CI_IMAGE_ANNOUNCE_ACTION:-}" ] && return 0

  # GITHUB_ENV reaches later steps; the file covers nested bash within this one.
  local marker="${CI_IMAGE_ANNOUNCED_MARKER:-/tmp/.ci-image-announced}"
  [ -n "${CI_IMAGE_ANNOUNCED:-}" ] && return 0
  [ -e "$marker" ] && return 0
  : > "$marker" 2>/dev/null || return 0
  [ -n "${GITHUB_ENV:-}" ] && echo "CI_IMAGE_ANNOUNCED=1" >> "$GITHUB_ENV"

  {
    echo "CI image"
    echo "  image:      $CI_IMAGE_NAME"
    echo "  tag:        ${CI_IMAGE_TAG:-<untagged>}"
    echo "  built:      ${CI_IMAGE_CREATED:-<unknown>}"
    [ -n "${CI_IMAGE_DOCKERFILE_URL:-}" ] && echo "  dockerfile: $CI_IMAGE_DOCKERFILE_URL"
    [ -n "${CI_INFRASTRUCTURE_BAKED_REF:-}" ] && echo "  ci-infra:   $CI_INFRASTRUCTURE_BAKED_REF"
  } >&2

  # Appended, never `>`: the summary is shared with every other step.
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
      echo "<details><summary>CI image — <code>$CI_IMAGE_NAME</code></summary>"
      echo
      echo "| | |"
      echo "|---|---|"
      echo "| image | \`$CI_IMAGE_NAME\` |"
      echo "| tag | \`${CI_IMAGE_TAG:-<untagged>}\` |"
      echo "| built | \`${CI_IMAGE_CREATED:-<unknown>}\` |"
      if [ -n "${CI_IMAGE_DOCKERFILE_URL:-}" ]; then
        echo "| dockerfile | [$CI_IMAGE_NAME/Dockerfile]($CI_IMAGE_DOCKERFILE_URL) |"
      fi
      if [ -n "${CI_INFRASTRUCTURE_BAKED_REF:-}" ]; then
        echo "| ci-infrastructure | \`$CI_INFRASTRUCTURE_BAKED_REF\` |"
      fi
      echo
      echo "</details>"
    } >> "$GITHUB_STEP_SUMMARY"
  fi

  return 0
}

# `|| true` is what suspends the sourcing shell's `set -e` for the whole body.
__ci_announce_image || true
unset -f __ci_announce_image
