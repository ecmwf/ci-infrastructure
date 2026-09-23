#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Build, test and push the CI images under public-images/.

Image discovery, tagging and OCI labels for images.yml and for hand builds alike.
Run it as ./build-image.sh. Stdlib only, and outside src/ (a tag path of every image).

Usage:
  ./build-image.sh --discover [--mode validate|validate-bases|publish] [--rebuild "all|<names>"]
  ./build-image.sh <platform>/<variant> [--push] [--force] [--require-clean]
  ./build-image.sh --test <platform>/<variant>
  ./build-image.sh --push-built <platform>/<variant> [--force] [--require-clean]
  ./build-image.sh --print-tag <platform>/<variant>

A build lands in the local docker daemon. images.yml runs --test against that
image and then --push-built, so what reaches the registry is exactly what was
tested; `<name> --push` is the untested shortcut for use by hand.

BASE_IMAGE=<ref> builds a dependent on a base loaded into the local daemon
instead of the published :latest, and makes --test prove that it did.

THERE IS EXACTLY ONE ANSWER TO "DOES THIS IMAGE NEED REBUILDING?"

  tag = short SHA of the last commit touching the image's build inputs
  rebuild <=> that tag is not in the registry

ROLLING PLATFORMS (public-images/rolling-*/) track upstream, so their tag also
carries a UTC date, <sha>-<YYYYMMDD>; a tag still names fixed bytes.

--discover and build share compute_tag; keep it the only rebuild rule.

Conventions (see docs/howto/images.rst):
  - image ref  = <REGISTRY>/<PROJECT>/<platform>-<variant>:<tag>
  - also tagged  <REGISTRY>/<PROJECT>/<platform>-<variant>:latest  (on push)
  - tag        = git log -1 --format=%h over the tag paths (below)
  - dependents = Dockerfiles that FROM a locally-published image; detected by
                 the FROM line referencing REGISTRY/PROJECT. They FROM :latest,
                 and their base's directory is part of their own identity.

TAG PATHS. An image's own directory, its base's if a dependent, and every
context path a Dockerfile reads (EXTRA_TAG_PATHS). Over-approximating is safe.

Env overrides: REGISTRY, PROJECT, IMAGES_DIR, IMAGE_TAG, IMAGE_SOURCE_REPO,
  BUILDX_BUILDER, BASE_IMAGE, PUBLIC_ECCR_ROBOT_NAME, PUBLIC_ECCR_ROBOT_TOKEN
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, NoReturn, cast

REGISTRY: Final = os.environ.get("REGISTRY") or "eccr.ecmwf.int"
PROJECT: Final = os.environ.get("PROJECT") or "public-ci-images"
IMAGES_DIR: Final = os.environ.get("IMAGES_DIR") or "public-images"
EXTRA_TAG_PATHS: Final = ("src", "pyproject.toml", "LICENSE", f"{IMAGES_DIR}/announce-image.sh")

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
REPO_PREFIX: Final = f"{REGISTRY}/{PROJECT}"

Mode = Literal["validate", "validate-bases", "publish"]
MODES: Final = ("validate", "validate-bases", "publish")


class BuildImageError(Exception):
    pass


def die(message: str) -> NoReturn:
    raise BuildImageError(message)


def say(message: str = "") -> None:
    print(message, flush=True)


def note(message: str) -> None:
    say(f"::notice::{message}")


def _env(name: str) -> str:
    return os.environ.get(name, "")


def _run(cmd: list[str], stdin: str | None = None) -> None:
    """Run with output streaming to ours; a failing command ends the script with its code."""
    rc = subprocess.run(cmd, input=stdin, text=True, check=False).returncode
    if rc != 0:
        raise SystemExit(rc)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=False
    ).stdout.strip()


# --- the image graph ----------------------------------------------------------


def enumerate_images() -> list[str]:
    """All images as "platform/variant", sorted. Adding a directory with a Dockerfile adds an image."""
    root = REPO_ROOT / IMAGES_DIR
    return sorted(f"{d.parent.name}/{d.name}" for d in root.glob("*/*") if (d / "Dockerfile").is_file())


def dockerfile_for(name: str) -> Path:
    return REPO_ROOT / IMAGES_DIR / name / "Dockerfile"


def first_from(name: str) -> str:
    """The image reference on the first FROM line."""
    dockerfile = dockerfile_for(name)
    lines = [line for line in dockerfile.read_text().splitlines() if re.match(r"\s*FROM\s", line, re.IGNORECASE)]
    if not lines:
        die(f"{name}: no FROM line in {dockerfile}")
    line = lines[0]
    # Multi-stage: the first FROM would be the builder stage.
    if re.search(r"\s[Aa][Ss]\s", line):
        die(f"{name}: multi-stage Dockerfiles are not supported (found '{line}')")
    if "$" in line:
        die(f"{name}: a variable FROM is not supported (found '{line}')")
    return line.split()[1]


def resolve_base(name: str) -> str:
    """The "platform/variant" this image FROMs when it is one of ours; "" for an upstream base."""
    from_line = first_from(name)
    if not from_line.startswith(f"{REPO_PREFIX}/"):
        if from_line.startswith(f"{REGISTRY}/"):
            die(f"{name} FROMs {from_line}, which is on {REGISTRY} but not in project {PROJECT}")
        return ""
    flat = from_line.rsplit(":", 1)[0].removeprefix(f"{REPO_PREFIX}/")
    # Reverse-map the flat name to a directory; the longest platform prefix wins.
    best = ""
    best_len = 0
    for pdir in sorted(p for p in (REPO_ROOT / IMAGES_DIR).glob("*") if p.is_dir()):
        platform = pdir.name
        if not flat.startswith(f"{platform}-"):
            continue
        variant = flat.removeprefix(f"{platform}-")
        if not (pdir / variant).is_dir():
            continue
        if len(platform) > best_len:
            best, best_len = f"{platform}/{variant}", len(platform)
    if not best:
        die(f"{name} FROMs '{flat}' but no matching {IMAGES_DIR}/*/*/ directory exists")
    return best


# --- identity -----------------------------------------------------------------


def tag_paths(name: str) -> list[str]:
    """Everything this image's tag is a function of."""
    paths = [f"{IMAGES_DIR}/{name}"]
    base = resolve_base(name)
    if base:
        paths.append(f"{IMAGES_DIR}/{base}")
    return [*paths, *EXTRA_TAG_PATHS]


def git_identity(name: str, fmt: str) -> str:
    """`git log -1` over the tag paths -- committed content, never HEAD, so a hand
    rebuild yields the SAME tag and a base and its dependents agree on the base's tag."""
    out = _git("log", "-1", f"--format={fmt}", "--", *tag_paths(name))
    if not out:
        die(f"could not determine the tag for {name} (shallow checkout? run with fetch-depth: 0)")
    return out


def is_rolling(name: str) -> bool:
    """A platform named `rolling` or `rolling-<distro>`; the prefix, so a second one needs no change here."""
    platform = name.split("/", 1)[0]
    return "/" in name and (platform == "rolling" or platform.startswith("rolling-"))


def compute_tag(name: str) -> str:
    """IMAGE_TAG wins outright: images.yml pins the tag discover computed, so a run
    crossing midnight UTC cannot discover <sha>-20260902 and publish <sha>-20260903."""
    if pinned := _env("IMAGE_TAG"):
        return pinned
    tag = git_identity(name, "%h")
    return f"{tag}-{datetime.now(UTC):%Y%m%d}" if is_rolling(name) else tag


def compute_revision(name: str) -> str:
    return git_identity(name, "%H")


def flat_name(name: str) -> str:
    return name.replace("/", "-")


def image_ref(name: str, tag: str) -> str:
    return f"{REPO_PREFIX}/{flat_name(name)}:{tag}"


# --- registry -----------------------------------------------------------------

_ABSENT: Final = ("not found", "NAME_UNKNOWN", "MANIFEST_UNKNOWN", "manifest unknown", "404")


def image_exists(ref: str) -> bool:
    """Queried straight from the registry. A missing tag and an unreachable registry
    must not look alike: absent => rebuild, anything else => hard error."""
    proc = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", ref],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return True
    out = proc.stdout.rstrip("\n")
    if any(marker in out for marker in _ABSENT):
        return False
    die(f"cannot reach {REGISTRY} to check {ref}: {out}")


def registry_login() -> None:
    user, token = _env("PUBLIC_ECCR_ROBOT_NAME"), _env("PUBLIC_ECCR_ROBOT_TOKEN")
    if not (user and token):
        return
    say(f"  login: {REGISTRY} as {user}")
    _run(["docker", "login", "--username", user, "--password-stdin", REGISTRY], stdin=token)


def require_buildx() -> None:
    if shutil.which("docker") is None:
        die("docker not found (needed to query the registry and build)")
    if subprocess.run(["docker", "buildx", "version"], capture_output=True, check=False).returncode != 0:
        die("docker buildx not found (install it, or use a current Docker)")


# --- discover -----------------------------------------------------------------


def discover(mode: Mode, rebuild: str) -> dict[str, object]:
    """The GitHub matrices for the images whose tag is not in the registry. On a pull
    request nothing is pushed, so "missing" means "what a merge would build"."""
    require_buildx()

    images = enumerate_images()
    if not images:
        die(f"no images found under {IMAGES_DIR}/*/*/Dockerfile")

    forced_names = rebuild.split()
    missing: list[str] = []
    for name in images:
        tag = compute_tag(name)
        if "all" in forced_names or name in forced_names:
            print(f"  {name}:{tag} -- forced rebuild", file=sys.stderr, flush=True)
        elif image_exists(image_ref(name, tag)):
            print(f"  {name}:{tag} -- already published, skipping", file=sys.stderr, flush=True)
            continue
        else:
            print(f"  {name}:{tag} -- missing, will build", file=sys.stderr, flush=True)
        missing.append(name)

    bases: list[str] = []
    dependents: list[str] = []
    for name in missing:
        base = resolve_base(name)
        if not base:
            bases.append(name)
            continue
        if resolve_base(base):
            die(
                f"{name} FROMs {base}, which is itself a dependent; images.yml builds all dependents in one "
                "parallel matrix, so chains deeper than base->variant are not supported"
            )
        # Otherwise it builds on the rebuilt base, handed over by images.yml.
        if mode == "validate-bases" and base in missing:
            note(f"skipping {name}: its base {base} is being rebuilt in this run (mode validate-bases)")
            continue
        dependents.append(name)

    exported = {b for b in (resolve_base(d) for d in dependents) if b in bases}
    base_matrix = {
        "include": [
            {
                "name": n,
                "tag": compute_tag(n),
                "flat": flat_name(n),
                "ref": image_ref(n, compute_tag(n)),
                "export": n in exported,
            }
            for n in bases
        ]
    }
    dependent_include = []
    for n in dependents:
        b = resolve_base(n)
        dependent_include.append(
            {
                "name": n,
                "tag": compute_tag(n),
                "base": flat_name(b),
                "base_ref": image_ref(b, compute_tag(b)) if b in bases else "",
            }
        )
    outputs: dict[str, object] = {
        "base-matrix": base_matrix,
        "dependent-matrix": {"include": dependent_include},
        "base-in-set": bool(bases),
    }

    lines = [
        f"base-matrix={json.dumps(base_matrix, separators=(',', ':'))}",
        f"dependent-matrix={json.dumps(outputs['dependent-matrix'], separators=(',', ':'))}",
        f"base-in-set={'true' if bases else 'false'}",
    ]
    say("\n".join(lines))
    # Append, never truncate: $GITHUB_OUTPUT is shared with every other step in the job.
    if github_output := _env("GITHUB_OUTPUT"):
        with open(github_output, "a") as fh:
            fh.write("\n".join(lines) + "\n")

    if summary := _env("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            fh.write(f"### Images to build (mode: {mode})\n\n")
            if not missing:
                fh.write(f"Nothing to do — every image's tag is already in `{REPO_PREFIX}`.\n")
            else:
                fh.write("| image | tag | role |\n|---|---|---|\n")
                fh.writelines(f"| `{n}` | `{compute_tag(n)}` | base |\n" for n in bases)
                fh.writelines(f"| `{n}` | `{compute_tag(n)}` | dependent |\n" for n in dependents)
    return outputs


# --- build --------------------------------------------------------------------

# The label facts again, as build args baked into CI_IMAGE_* for jobs inside the image.
# Passed only to Dockerfiles that declare them; not identity.
BUILD_ARGS: Final = ("SOURCE_REVISION", "IMAGE_NAME", "IMAGE_TAG", "IMAGE_CREATED", "IMAGE_DOCKERFILE_URL")


def declared_args(dockerfile: Path) -> list[str]:
    text = dockerfile.read_text()
    return [a for a in BUILD_ARGS if re.search(rf"^\s*ARG\s+{a}(\s|=|$)", text, re.MULTILINE)]


def source_repo() -> str:
    if repo := _env("IMAGE_SOURCE_REPO") or _env("GITHUB_REPOSITORY"):
        return repo
    remote = _git("remote", "get-url", "origin")
    remote = re.sub(r"^git@[^:]+:", "", remote)
    remote = re.sub(r"^https?://[^/]+/", "", remote)
    return re.sub(r"\.git$", "", remote)


def build(name: str, *, push: bool, force: bool, require_clean: bool) -> None:
    require_buildx()
    dockerfile = dockerfile_for(name)
    if not dockerfile.is_file():
        die(f"no Dockerfile at {IMAGES_DIR}/{name}/")

    base = resolve_base(name)
    tag = compute_tag(name)
    revision = compute_revision(name)
    ref = image_ref(name, tag)
    base_image = _env("BASE_IMAGE")

    say(f"  image:    {name}")
    if base:
        say(f"  base:     {base_image or image_ref(base, 'latest')}")
    elif base_image:
        die(f"BASE_IMAGE is set but {name} is not a dependent")
    say(f"  tag:      {tag} ({revision})")
    say(f"  inputs:   {' '.join(tag_paths(name))}")

    check_clean(name, require_clean=require_clean)

    registry_login()  # buildkit pulls the base from the registry
    if push and not force and image_exists(ref):
        note(f"{ref} already exists — skipping build+push")
        return

    # OCI labels: https://specs.opencontainers.org/image-spec/annotations/
    source_url = f"{_env('GITHUB_SERVER_URL') or 'https://github.com'}/{source_repo()}"
    created = f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}"
    dockerfile_url = f"{source_url}/blob/{revision}/{IMAGES_DIR}/{name}/Dockerfile"
    labels = {
        "org.opencontainers.image.source": source_url,
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.created": created,
        "org.opencontainers.image.title": name,
        "org.opencontainers.image.description": f"CI image {name} built from {IMAGES_DIR}/{name}/Dockerfile",
        "int.ecmwf.ci.dockerfile": dockerfile_url,
    }
    if base:
        labels["org.opencontainers.image.base.name"] = image_ref(base, "latest")

    values = {
        "SOURCE_REVISION": revision,
        "IMAGE_NAME": name,
        "IMAGE_TAG": tag,
        "IMAGE_CREATED": created,
        "IMAGE_DOCKERFILE_URL": dockerfile_url,
    }

    cmd = ["docker", "buildx", "build"]
    if builder := _env("BUILDX_BUILDER"):
        cmd += ["--builder", builder]
    cmd += ["--file", str(dockerfile)]
    for key, value in labels.items():
        cmd += ["--label", f"{key}={value}"]
    for arg in declared_args(dockerfile):
        cmd += ["--build-arg", f"{arg}={values[arg]}"]
    if base_image:
        cmd += ["--build-context", f"{image_ref(base, 'latest')}=docker-image://{base_image}"]
    cmd += ["-t", ref, "--load", str(REPO_ROOT)]

    say(f"Building {ref}")
    _run(cmd)
    note(f"built {ref} (loaded, not pushed)")
    if push:
        push_built(name, force=force, require_clean=require_clean)


def check_clean(name: str, *, require_clean: bool) -> None:
    """The tag is a function of committed content but the build uses the working
    tree, so a dirty tree can mint a tag whose content is not what is committed."""
    paths = tag_paths(name)
    diff = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--quiet", "HEAD", "--", *paths],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if diff.returncode == 0:
        return
    if require_clean:
        die(f"{name} has uncommitted changes in {' '.join(paths)}; refusing to publish")
    say(f"::warning::{' '.join(paths)} has uncommitted changes; image {name} may not match the committed Dockerfile")


def _require_local(ref: str) -> None:
    inspect = subprocess.run(["docker", "image", "inspect", ref], capture_output=True, check=False)
    if inspect.returncode != 0:
        die(f"{ref} is not in the local daemon; build it first")


def push_built(name: str, *, force: bool, require_clean: bool) -> None:
    """Push the image a previous build loaded, never a rebuild of it: a rebuild of a
    rolling image, or of anything installing unpinned packages, is not what was tested."""
    require_buildx()
    ref = image_ref(name, compute_tag(name))
    latest_ref = image_ref(name, "latest")
    _require_local(ref)
    check_clean(name, require_clean=require_clean)
    registry_login()
    if not force and image_exists(ref):
        note(f"{ref} already exists — skipping push")
        return
    _run(["docker", "tag", ref, latest_ref])
    _run(["docker", "push", ref])
    _run(["docker", "push", latest_ref])
    note(f"pushed {ref} and {latest_ref}")


def _layers(ref: str) -> list[str]:
    proc = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .RootFS.Layers}}", ref],
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    layers: list[str] = json.loads(proc.stdout)
    return layers


# Runs inside the image, against the BAKED package: the import check fails when
# ci_infrastructure would come from the mounted checkout instead.
_IN_IMAGE_PYTEST: Final = """
    set -euo pipefail
    "$CI_INFRASTRUCTURE_PYTHON" -c "import ci_infrastructure, sys; f = ci_infrastructure.__file__; print(sys.version.split()[0], f); sys.exit(f.startswith(\\"/repo/\\"))"
    "$CI_INFRASTRUCTURE_PYTHON" -m pytest -p no:cacheprovider /repo/tests"""  # noqa: E501


def test_image(name: str) -> None:
    """The image contract and the test suite, inside the locally built image, with the
    checkout mounted read-only."""
    require_buildx()
    ref = image_ref(name, compute_tag(name))
    _require_local(ref)

    if base_image := _env("BASE_IMAGE"):
        base_layers = _layers(base_image)
        own_layers = _layers(ref)
        if len(own_layers) <= len(base_layers) or own_layers[: len(base_layers)] != base_layers:
            die(f"{ref} was not built on {base_image}: its layers do not start with the base's")
        say(f"  base:     {base_image} (layers verified)")

    mount = f"{REPO_ROOT}:/repo:ro"
    say(f"::group::image contract ({name})")
    _run(["docker", "run", "--rm", "-v", mount, ref, "bash", f"/repo/{IMAGES_DIR}/verify-image.sh", name])
    say("::endgroup::")

    say(f"::group::pytest in {name}")
    _run(
        ["docker", "run", "--rm", "-e", "PYTHONDONTWRITEBYTECODE=1", "-w", "/tmp", "-v", mount, ref]
        + ["bash", "-c", _IN_IMAGE_PYTEST]
    )
    say("::endgroup::")
    note(f"tested {ref}")


# --- args ---------------------------------------------------------------------


def main(argv: list[str]) -> None:
    action = "build"
    name = ""
    mode = "publish"
    rebuild = ""
    push = force = require_clean = False

    args = iter(argv)
    for arg in args:
        match arg:
            case "--discover":
                action = "discover"
            case "--print-tag":
                action = "print-tag"
            case "--test":
                action = "test"
            case "--push-built":
                action = "push-built"
            case "--mode":
                mode = next(args, "")
            case "--rebuild":
                rebuild = next(args, "")
            case "--push":
                push = True
            case "--force":
                force = True
            case "--require-clean":
                require_clean = True
            case "-h" | "--help":
                say(__doc__ or "")
                return
            case _ if arg.startswith("-"):
                die(f"unknown flag: {arg}")
            case _:
                if name:
                    die(f"unexpected extra argument: {arg}")
                name = arg

    if action == "discover":
        if mode not in MODES:
            die(f"--mode must be validate, validate-bases or publish, got '{mode}'")
        if name:
            die("--discover takes no image argument")
        discover(cast(Mode, mode), rebuild)
    elif not name:
        die(
            {
                "print-tag": "--print-tag needs an image: <platform>/<variant>",
                "test": "--test needs an image: <platform>/<variant>",
                "push-built": "--push-built needs an image: <platform>/<variant>",
                "build": "usage: build-image.sh <platform>/<variant> [--push] [--force] [--require-clean]",
            }[action]
        )
    elif action == "print-tag":
        say(compute_tag(name))
    elif action == "test":
        test_image(name)
    elif action == "push-built":
        push_built(name, force=force, require_clean=require_clean)
    else:
        build(name, push=push, force=force, require_clean=require_clean)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except BuildImageError as exc:
        print(f"::error::{exc}", file=sys.stderr, flush=True)
        sys.exit(1)
