#!/usr/bin/env bash
#
# build-image.sh — build (and optionally push) the CI images under public-images/.
#
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
#
# Single source of truth for image discovery, tagging and OCI labels, used by
# .github/workflows/images.yml AND by hand on a workstation, so the two produce
# byte-identical, identically-tagged images.
#
# Usage:
#   ./build-image.sh --discover [--mode validate|validate-bases|publish] [--rebuild "all|<names>"]
#   ./build-image.sh <platform>/<variant> [--push] [--force] [--require-clean]
#   ./build-image.sh --test <platform>/<variant>
#   ./build-image.sh --push-built <platform>/<variant> [--force] [--require-clean]
#   ./build-image.sh --print-tag <platform>/<variant>
#
# A build lands in the local docker daemon. images.yml runs --test against that
# image and then --push-built, so what reaches the registry is exactly what was
# tested; `<name> --push` is the untested shortcut for use by hand.
#
# BASE_IMAGE=<ref> builds a dependent on a base loaded into the local daemon
# instead of the published :latest, and makes --test prove that it did.
#
# THERE IS EXACTLY ONE ANSWER TO "DOES THIS IMAGE NEED REBUILDING?"
#
#   tag = short SHA of the last commit touching the image's build inputs
#   rebuild <=> that tag is not in the registry
#
# ROLLING PLATFORMS bend the first line, never the second. An image under
# public-images/rolling-*/ tracks upstream continuously, so its content is NOT a
# function of our git history: the same commit yields a different image every
# night. Its tag therefore carries a UTC date as well -- <sha>-<YYYYMMDD> -- and
# the rule above then does the right thing on its own, because each night's tag
# is genuinely new and genuinely absent from the registry. Note what this is NOT:
# it is not a second answer to the rebuild question, and it is not a forced
# rebuild that republishes one tag with different content. Both would break the
# guarantee that a tag names fixed bytes.
#
# --discover and the build path compute that tag through the same functions
# below, which is the whole point of this file. Do not reintroduce a second
# mechanism -- not a `git diff`, not a workflow `paths:` filter, not a
# hand-maintained list of images. A second answer drifts from this one, and when
# it does the build is skipped, a dependent's :latest keeps pointing at an image
# built on the previous base, and CI goes green on a stale image.
#
# Conventions (see IMAGES.md):
#   - image ref  = <REGISTRY>/<PROJECT>/<platform>-<variant>:<tag>
#   - also tagged  <REGISTRY>/<PROJECT>/<platform>-<variant>:latest  (on push)
#   - tag        = git log -1 --format=%h over TAG_PATHS (below)
#   - dependents = Dockerfiles that FROM a locally-published image; detected by
#                  the FROM line referencing REGISTRY/PROJECT. They FROM :latest,
#                  and their base's directory is part of their own identity.
#
# TAG PATHS. An image's identity is its own directory, its base's directory if it
# is a dependent, and every path outside its directory that a Dockerfile reads
# from the build context (src/, pyproject.toml, LICENSE, announce-image.sh).
# Edit EXTRA_TAG_PATHS when that set changes. actions/, runners/ and tests/ are
# deliberately absent: actions are fetched by GitHub at job time and never baked,
# runners/ configures the runner rather than the image, tests are not installed.
#
# This deliberately OVER-approximates: an image that installs no
# ci-infrastructure still retags on every src/ commit. Over-building is safe and
# under-building is not, so accept it rather than probing each Dockerfile.
#
# Env overrides: REGISTRY, PROJECT, IMAGE_TAG, IMAGE_SOURCE_REPO, BUILDX_BUILDER,
#   PUBLIC_ECCR_ROBOT_NAME, PUBLIC_ECCR_ROBOT_TOKEN
set -euo pipefail

REGISTRY="${REGISTRY:-eccr.ecmwf.int}"
PROJECT="${PROJECT:-public-ci-images}"
IMAGES_DIR="${IMAGES_DIR:-public-images}"
EXTRA_TAG_PATHS="src pyproject.toml LICENSE $IMAGES_DIR/announce-image.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
REPO_PREFIX="$REGISTRY/$PROJECT"

die() { echo "::error::$*" >&2; exit 1; }
note() { echo "::notice::$*"; }

# Bash 3.2 compatible on purpose (no associative arrays, no mapfile) so the whole
# thing can be dry-run identically on a macOS workstation.

# --- the image graph ----------------------------------------------------------

# All images, one "platform/variant" per line, sorted. No list to maintain
# anywhere: adding a directory with a Dockerfile is what adds an image.
enumerate_images() {
  local d rel
  for d in "$REPO_ROOT/$IMAGES_DIR"/*/*/; do
    [ -f "$d/Dockerfile" ] || continue
    rel="${d%/}"; rel="${rel#"$REPO_ROOT/$IMAGES_DIR/"}"
    echo "$rel"
  done | sort
}

dockerfile_for() { echo "$REPO_ROOT/$IMAGES_DIR/$1/Dockerfile"; }

# The image reference on the first FROM line.
first_from() {
  local dockerfile line
  dockerfile="$(dockerfile_for "$1")"
  line="$(grep -iE '^[[:space:]]*FROM[[:space:]]' "$dockerfile" | head -1)"
  [ -n "$line" ] || die "$1: no FROM line in $dockerfile"
  # Multi-stage would make `head -1` silently pick the builder stage, and the
  # base resolution below would then be about the wrong image. Refuse instead.
  case "$line" in
    *[[:space:]][Aa][Ss][[:space:]]*) die "$1: multi-stage Dockerfiles are not supported (found '$line')" ;;
  esac
  case "$line" in
    *'$'*) die "$1: a variable FROM is not supported (found '$line')" ;;
  esac
  echo "$line" | awk '{print $2}'
}

# The "platform/variant" this image FROMs, when that is one of ours; empty when
# it FROMs an upstream image such as ubuntu:24.04.
resolve_base() {
  local name from_line base_repo flat pdir p v best="" best_len=0
  name="$1"
  from_line="$(first_from "$name")"
  case "$from_line" in
    "$REPO_PREFIX/"*) ;;
    "$REGISTRY/"*) die "$name FROMs $from_line, which is on $REGISTRY but not in project $PROJECT" ;;
    *) return 0 ;;   # upstream base (ubuntu:24.04) -- not ours, not our identity
  esac
  base_repo="${from_line%:*}"          # strip :latest (or any tag)
  flat="${base_repo#"$REPO_PREFIX/"}"  # e.g. ubuntu24.04-base
  # Reverse-map the flat registry name to a directory.
  for pdir in "$REPO_ROOT/$IMAGES_DIR"/*/; do
    [ -d "$pdir" ] || continue
    p="${pdir%/}"; p="${p##*/}"
    case "$flat" in "${p}-"*) ;; *) continue ;; esac
    v="${flat#"${p}-"}"
    [ -d "$REPO_ROOT/$IMAGES_DIR/$p/$v" ] || continue
    # Longest PLATFORM prefix wins, so platforms 'ubuntu24.04' and
    # 'ubuntu24.04-x' cannot both claim 'ubuntu24.04-x-base'. Compare against the
    # winning platform's length, not the length of "platform/variant".
    if [ "${#p}" -gt "$best_len" ]; then best="$p/$v"; best_len="${#p}"; fi
  done
  [ -n "$best" ] || die "$name FROMs '$flat' but no matching $IMAGES_DIR/*/*/ directory exists"
  echo "$best"
}

# --- identity -----------------------------------------------------------------

# Everything this image's tag is a function of.
tag_paths() {
  local name base
  name="$1"
  echo "$IMAGES_DIR/$name"
  base="$(resolve_base "$name")"
  # A base change changes a dependent's effective content, so the base directory
  # is part of the dependent's identity too.
  [ -n "$base" ] && echo "$IMAGES_DIR/$base"
  # shellcheck disable=SC2086  # deliberate word splitting of the path list
  printf '%s\n' $EXTRA_TAG_PATHS
}

# `git log -1` over the tag paths -- committed content, never HEAD, so a hand
# rebuild yields the SAME tag (idempotent) and a base and its dependents always
# agree on the base's tag. $1 = image, $2 = %h or %H.
_git_identity() {
  local name fmt paths out
  name="$1"; fmt="$2"
  paths=()
  while IFS= read -r p; do [ -n "$p" ] && paths+=("$p"); done < <(tag_paths "$name")
  out="$(git -C "$REPO_ROOT" log -1 --format="$fmt" -- "${paths[@]}")"
  [ -n "$out" ] || die "could not determine the tag for $name (shallow checkout? run with fetch-depth: 0)"
  echo "$out"
}

# Rolling platforms are identified by the platform component of their path, so
# there is still no list to maintain -- the same reason discovery is a glob. A
# platform named `rolling` or `rolling-<distro>` is the whole rule; see the header
# and IMAGES.md. The prefix, not the exact name, so a second rolling platform
# (rolling-fedora, say) needs no change here.
is_rolling() {
  case "$1" in rolling/*|rolling-*/*) return 0 ;; *) return 1 ;; esac
}

# A rolling image's tag gets a UTC date suffix, because its content changes
# without a commit. IMAGE_TAG still wins outright: images.yml pins the tag
# discover computed, so a run that crosses midnight UTC cannot have the discover
# job decide to build <sha>-20260902 and the build job then publish
# <sha>-20260903.
compute_tag() {
  local t
  [ -n "${IMAGE_TAG:-}" ] && { echo "$IMAGE_TAG"; return 0; }
  t="$(_git_identity "$1" %h)"
  if is_rolling "$1"; then
    echo "$t-$(date -u +%Y%m%d)"
  else
    echo "$t"
  fi
}

compute_revision() { _git_identity "$1" %H; }

flat_name() { echo "${1//\//-}"; }   # ubuntu24.04/base -> ubuntu24.04-base
in_list() { case " $2 " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
image_ref() { echo "$REPO_PREFIX/$(flat_name "$1"):$2"; }

# --- registry -----------------------------------------------------------------

# ONE implementation, queried straight from the registry. buildx is required:
# it is on every GitHub-hosted runner and on any current Docker install, and
# picking it here is what let the buildah/skopeo/docker-manifest fan-out go.
#
# A missing tag and an unreachable registry must not look alike. Failing open
# would rebuild everything and then fail at push; failing closed would silently
# skip a push that was needed. So: absent => rebuild, anything else => hard error.
image_exists() {
  local ref out rc
  ref="$1"
  set +e
  out="$(docker buildx imagetools inspect "$ref" 2>&1)"; rc=$?
  set -e
  [ $rc -eq 0 ] && return 0
  case "$out" in
    *"not found"*|*"NAME_UNKNOWN"*|*"MANIFEST_UNKNOWN"*|*"manifest unknown"*|*"404"*) return 1 ;;
  esac
  die "cannot reach $REGISTRY to check $ref: $out"
}

registry_login() {
  local user="${PUBLIC_ECCR_ROBOT_NAME:-}" token="${PUBLIC_ECCR_ROBOT_TOKEN:-}"
  [ -n "$user" ] && [ -n "$token" ] || return 0
  echo "  login: $REGISTRY as $user"
  printf '%s' "$token" | docker login --username "$user" --password-stdin "$REGISTRY"
}

require_buildx() {
  command -v docker >/dev/null 2>&1 || die "docker not found (needed to query the registry and build)"
  docker buildx version >/dev/null 2>&1 || die "docker buildx not found (install it, or use a current Docker)"
}

# --- discover -----------------------------------------------------------------
#
# Emits the GitHub matrix JSON for the images whose tag is not in the registry.
# On a pull request nothing is pushed, so "missing" means "this is what a merge
# would build" -- which is exactly the set worth validating.
discover() {
  local mode="${1:-publish}" rebuild="${2:-}"
  local name base tag forced missing_names="" base_names="" dep_names="" base_in_set=false

  require_buildx

  # Discovery is a glob, so an empty tree is indistinguishable from "nothing to
  # do" in the matrices -- and a run that quietly builds nothing would look
  # green. If the directory is gone, say so.
  [ -n "$(enumerate_images)" ] || die "no images found under $IMAGES_DIR/*/*/Dockerfile"

  while IFS= read -r name; do
    [ -n "$name" ] || continue
    tag="$(compute_tag "$name")"
    forced=false
    case " $rebuild " in
      *" all "*)      forced=true ;;
      *" $name "*)    forced=true ;;
    esac
    if $forced; then
      echo "  $name:$tag -- forced rebuild" >&2
    elif image_exists "$(image_ref "$name" "$tag")"; then
      echo "  $name:$tag -- already published, skipping" >&2
      continue
    else
      echo "  $name:$tag -- missing, will build" >&2
    fi
    missing_names="$missing_names $name"
  done < <(enumerate_images)

  for name in $missing_names; do
    base="$(resolve_base "$name")"
    if [ -z "$base" ]; then
      base_names="$base_names $name"
      base_in_set=true
      continue
    fi
    # Guard the two-job model: build-base then build-dependents in one parallel
    # matrix cannot order a variant-on-variant chain.
    if [ -n "$(resolve_base "$base")" ]; then
      die "$name FROMs $base, which is itself a dependent; images.yml builds all dependents in one parallel matrix, so chains deeper than base->variant are not supported"
    fi
    # A dependent whose base is rebuilt in this run builds on that base, handed
    # over by images.yml as an artifact. validate-bases skips such dependents
    # instead, trading coverage for time.
    if [ "$mode" = validate-bases ] && in_list "$base" "$missing_names"; then
      note "skipping $name: its base $base is being rebuilt in this run (mode validate-bases)"
      continue
    fi
    dep_names="$dep_names $name"
  done

  local exported=""
  for name in $dep_names; do
    base="$(resolve_base "$name")"
    in_list "$base" "$base_names" && exported="$exported $base"
  done

  base_json() {
    local first=true n exported_flag
    printf '{"include":['
    for n in $1; do
      $first || printf ','; first=false
      exported_flag=false; in_list "$n" "$exported" && exported_flag=true
      printf '{"name":"%s","tag":"%s","flat":"%s","ref":"%s","export":%s}' \
        "$n" "$(compute_tag "$n")" "$(flat_name "$n")" "$(image_ref "$n" "$(compute_tag "$n")")" "$exported_flag"
    done
    printf ']}'
  }

  dependent_json() {
    local first=true n b base_ref
    printf '{"include":['
    for n in $1; do
      $first || printf ','; first=false
      b="$(resolve_base "$n")"
      base_ref=""; in_list "$b" "$base_names" && base_ref="$(image_ref "$b" "$(compute_tag "$b")")"
      printf '{"name":"%s","tag":"%s","base":"%s","base_ref":"%s"}' \
        "$n" "$(compute_tag "$n")" "$(flat_name "$b")" "$base_ref"
    done
    printf ']}'
  }

  {
    echo "base-matrix=$(base_json "$base_names")"
    echo "dependent-matrix=$(dependent_json "$dep_names")"
    echo "base-in-set=$base_in_set"
  # tee -a, never plain tee: $GITHUB_OUTPUT is an append-only file shared with
  # every other step in the job, and truncating it would silently discard
  # outputs written before us.
  } | tee -a "${GITHUB_OUTPUT:-/dev/null}"

  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
      echo "### Images to build (mode: $mode)"
      echo
      if [ -z "$missing_names" ]; then
        echo "Nothing to do — every image's tag is already in \`$REPO_PREFIX\`."
      else
        echo "| image | tag | role |"
        echo "|---|---|---|"
        for name in $base_names; do echo "| \`$name\` | \`$(compute_tag "$name")\` | base |"; done
        for name in $dep_names;  do echo "| \`$name\` | \`$(compute_tag "$name")\` | dependent |"; done
      fi
    } >> "$GITHUB_STEP_SUMMARY"
  fi
}

# --- build --------------------------------------------------------------------

build_one() {
  local name="$1" push="$2" force="$3" require_clean="$4"
  local dockerfile base tag revision ref paths=()

  require_buildx
  dockerfile="$(dockerfile_for "$name")"
  [ -f "$dockerfile" ] || die "no Dockerfile at $IMAGES_DIR/$name/"

  base="$(resolve_base "$name")"
  tag="$(compute_tag "$name")"
  revision="$(compute_revision "$name")"
  ref="$(image_ref "$name" "$tag")"

  while IFS= read -r p; do [ -n "$p" ] && paths+=("$p"); done < <(tag_paths "$name")

  echo "  image:    $name"
  if [ -n "$base" ]; then
    echo "  base:     ${BASE_IMAGE:-$(image_ref "$base" latest)}"
  elif [ -n "${BASE_IMAGE:-}" ]; then
    die "BASE_IMAGE is set but $name is not a dependent"
  fi
  echo "  tag:      $tag ($revision)"
  echo "  inputs:   ${paths[*]}"

  check_clean "$name" "$require_clean"

  registry_login                       # buildkit pulls the base from the registry
  if $push && ! $force && image_exists "$ref"; then
    note "$ref already exists — skipping build+push"
    return 0
  fi

  # OCI labels: https://specs.opencontainers.org/image-spec/annotations/
  local source_repo server_url source_url created dockerfile_url labels=()
  source_repo="${IMAGE_SOURCE_REPO:-${GITHUB_REPOSITORY:-}}"
  if [ -z "$source_repo" ]; then
    # `|| true`: a checkout with no origin (or no remote at all) is a normal
    # workstation case, and pipefail would otherwise abort the whole build here.
    local remote
    remote="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
    source_repo="$(printf '%s' "$remote" | sed -E 's#^git@[^:]+:##; s#^https?://[^/]+/##; s#\.git$##')"
  fi
  server_url="${GITHUB_SERVER_URL:-https://github.com}"
  source_url="$server_url/$source_repo"
  created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  dockerfile_url="$source_url/blob/$revision/$IMAGES_DIR/$name/Dockerfile"
  labels=(
    --label "org.opencontainers.image.source=$source_url"
    --label "org.opencontainers.image.revision=$revision"
    --label "org.opencontainers.image.created=$created"
    --label "org.opencontainers.image.title=$name"
    --label "org.opencontainers.image.description=CI image $name built from $IMAGES_DIR/$name/Dockerfile"
    --label "int.ecmwf.ci.dockerfile=$dockerfile_url"
  )
  [ -n "$base" ] && labels+=(--label "org.opencontainers.image.base.name=$(image_ref "$base" latest)")

  # Build args. Every value here is ALSO a label above -- the difference is that a
  # label can only be read from OUTSIDE the image (registry or daemon), and a job
  # running inside it has neither. So the same facts are baked in as CI_IMAGE_*
  # environment, which actions/announce-image prints. See IMAGES.md.
  #
  # Each is passed only to an image that declares the ARG, so one that does not
  # (a future image, or the private repo's) still builds without an "unused build
  # arg" warning. None of this is identity: identity is TAG_PATHS and nothing else.
  local build_args=() a
  for a in SOURCE_REVISION IMAGE_NAME IMAGE_TAG IMAGE_CREATED IMAGE_DOCKERFILE_URL; do
    grep -qE "^[[:space:]]*ARG[[:space:]]+$a([[:space:]]|=|\$)" "$dockerfile" || continue
    case "$a" in
      SOURCE_REVISION)      build_args+=(--build-arg "$a=$revision") ;;
      IMAGE_NAME)           build_args+=(--build-arg "$a=$name") ;;
      IMAGE_TAG)            build_args+=(--build-arg "$a=$tag") ;;
      IMAGE_CREATED)        build_args+=(--build-arg "$a=$created") ;;
      IMAGE_DOCKERFILE_URL) build_args+=(--build-arg "$a=$dockerfile_url") ;;
    esac
  done

  local context_args=()
  [ -n "${BASE_IMAGE:-}" ] && context_args=(--build-context "$(image_ref "$base" latest)=docker-image://$BASE_IMAGE")

  echo "Building $ref"
  docker buildx build \
    ${BUILDX_BUILDER:+--builder "$BUILDX_BUILDER"} \
    --file "$dockerfile" \
    "${labels[@]}" \
    ${build_args[@]+"${build_args[@]}"} \
    ${context_args[@]+"${context_args[@]}"} \
    -t "$ref" \
    --load \
    "$REPO_ROOT"

  note "built $ref (loaded, not pushed)"
  if $push; then push_built "$name" "$force" "$require_clean"; fi
}

# The tag is a function of committed content but the build uses the working
# tree, so a dirty tree can mint a tag whose content is not what is committed.
check_clean() {
  local name="$1" require_clean="$2" paths=()
  while IFS= read -r p; do [ -n "$p" ] && paths+=("$p"); done < <(tag_paths "$name")
  git -C "$REPO_ROOT" diff --quiet HEAD -- "${paths[@]}" 2>/dev/null && return 0
  $require_clean && die "$name has uncommitted changes in ${paths[*]}; refusing to publish"
  echo "::warning::${paths[*]} has uncommitted changes; image $name may not match the committed Dockerfile"
}

# Push the image a previous build loaded, never a rebuild of it: a rebuild of a
# rolling image, or of anything that installs unpinned packages, is not the
# image that was tested.
push_built() {
  local name="$1" force="$2" require_clean="$3" ref latest_ref
  require_buildx
  ref="$(image_ref "$name" "$(compute_tag "$name")")"
  latest_ref="$(image_ref "$name" latest)"
  docker image inspect "$ref" >/dev/null 2>&1 || die "$ref is not in the local daemon; build it first"
  check_clean "$name" "$require_clean"
  registry_login
  if ! $force && image_exists "$ref"; then
    note "$ref already exists — skipping push"
    return 0
  fi
  docker tag "$ref" "$latest_ref"
  docker push "$ref"
  docker push "$latest_ref"
  note "pushed $ref and $latest_ref"
}

# Run the image contract and the test suite inside the locally built image. The
# checkout is mounted read-only and the tests run against the BAKED package, so
# a broken install cannot hide behind the sources next to the tests.
test_image() {
  local name="$1" ref base
  require_buildx
  ref="$(image_ref "$name" "$(compute_tag "$name")")"
  docker image inspect "$ref" >/dev/null 2>&1 || die "$ref is not in the local daemon; build it first"

  if [ -n "${BASE_IMAGE:-}" ]; then
    local base_layers own_layers
    base_layers="$(docker image inspect --format '{{json .RootFS.Layers}}' "$BASE_IMAGE")"
    own_layers="$(docker image inspect --format '{{json .RootFS.Layers}}' "$ref")"
    case "$own_layers" in
      "${base_layers%]},"*) echo "  base:     $BASE_IMAGE (layers verified)" ;;
      *) die "$ref was not built on $BASE_IMAGE: its layers do not start with the base's" ;;
    esac
  fi

  echo "::group::image contract ($name)"
  docker run --rm -v "$REPO_ROOT:/repo:ro" "$ref" bash "/repo/$IMAGES_DIR/verify-image.sh" "$name"
  echo "::endgroup::"

  echo "::group::pytest in $name"
  # shellcheck disable=SC2016  # expanded inside the container
  docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -w /tmp -v "$REPO_ROOT:/repo:ro" "$ref" bash -c '
    set -euo pipefail
    "$CI_INFRASTRUCTURE_PYTHON" -c "import ci_infrastructure, sys; f = ci_infrastructure.__file__; print(sys.version.split()[0], f); sys.exit(f.startswith(\"/repo/\"))"
    "$CI_INFRASTRUCTURE_PYTHON" -m pytest -p no:cacheprovider /repo/tests'
  echo "::endgroup::"
  note "tested $ref"
}

# --- args ---------------------------------------------------------------------

ACTION=build
NAME=""
MODE=publish
REBUILD=""
PUSH=false
FORCE=false
REQUIRE_CLEAN=false

while [ $# -gt 0 ]; do
  case "$1" in
    --discover)      ACTION=discover ;;
    --print-tag)     ACTION=print-tag ;;
    --test)          ACTION="test" ;;
    --push-built)    ACTION="push-built" ;;
    --mode)          MODE="${2:-}"; shift ;;
    --rebuild)       REBUILD="${2:-}"; shift ;;
    --push)          PUSH=true ;;
    --force)         FORCE=true ;;
    --require-clean) REQUIRE_CLEAN=true ;;
    -h|--help)       sed -n '2,60p' "$0"; exit 0 ;;
    -*)              die "unknown flag: $1" ;;
    *)               [ -z "$NAME" ] || die "unexpected extra argument: $1"; NAME="$1" ;;
  esac
  shift
done

case "$ACTION" in
  discover)
    case "$MODE" in validate|validate-bases|publish) ;; *) die "--mode must be validate, validate-bases or publish, got '$MODE'" ;; esac
    [ -z "$NAME" ] || die "--discover takes no image argument"
    discover "$MODE" "$REBUILD"
    ;;
  print-tag)
    [ -n "$NAME" ] || die "--print-tag needs an image: <platform>/<variant>"
    compute_tag "$NAME"
    ;;
  test)
    [ -n "$NAME" ] || die "--test needs an image: <platform>/<variant>"
    test_image "$NAME"
    ;;
  push-built)
    [ -n "$NAME" ] || die "--push-built needs an image: <platform>/<variant>"
    push_built "$NAME" "$FORCE" "$REQUIRE_CLEAN"
    ;;
  build)
    [ -n "$NAME" ] || die "usage: build-image.sh <platform>/<variant> [--push] [--force] [--require-clean]"
    build_one "$NAME" "$PUSH" "$FORCE" "$REQUIRE_CLEAN"
    ;;
esac
