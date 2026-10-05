# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Several packages per repo, deps on packages of the same repo, and meta (umbrella) packages."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import parse_all, write_repo

from ci_infrastructure import resolve_deps
from ci_infrastructure._github_api import EXECUTION_RUNNER, ManifestSchemaError
from ci_infrastructure.generate_downstream_ci import SchemaError, derive_cross_repo_needs, validate_graph
from ci_infrastructure.manifest import validate
from ci_infrastructure.resolve_deps import (
    DepSpec,
    Manifest,
    PackageName,
    PackageSpec,
    Ref,
    Repo,
    ResolveError,
    Sha,
    parse_manifest,
    resolve_leg,
)

_LEG: Final = {"platform": "ubuntu-24.04", "cxx-compiler": "g++-13", "build-type": "Release"}

# stack-deps as an umbrella over three libraries; proj needs sqlite3.
_STACK: Final = """
[package]
name = "stack-deps"
repo = "o/stack"
compiler-inputs = ["cxx-compiler"]
meta = true

[[deps]]
package = ["libaec", "sqlite3", "proj"]

[packages.libaec]
compiler-inputs = ["cxx-compiler"]
[packages.sqlite3]
compiler-inputs = ["cxx-compiler"]
[packages.proj]
compiler-inputs = ["cxx-compiler"]
deps = [{ package = "sqlite3" }]

[matrix.libaec]
packages = ["libaec"]
[[matrix.libaec.include]]
platform = "ubuntu-24.04"
cxx-compiler = "g++-13"
[matrix.sqlite3]
packages = ["sqlite3"]
reuse-matrix = "libaec"
[matrix.proj]
packages = ["proj"]
reuse-matrix = "libaec"
"""


def _schema(body: str) -> Any:
    import tomllib

    return validate(tomllib.loads(textwrap.dedent(body)))


# --- schema -------------------------------------------------------------------------------------------------


def test_the_umbrella_manifest_validates_and_kinds_publish_their_package() -> None:
    raw = _schema(_STACK)
    assert raw.package.meta
    assert {k: raw.published_by(k) for k in raw.matrix} == {
        "libaec": ("libaec",),
        "sqlite3": ("sqlite3",),
        "proj": ("proj",),
    }


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        (('deps = [{ package = "sqlite3" }]', 'deps = [{ package = "nope" }]'), "declares no such package"),
        (('deps = [{ package = "sqlite3" }]', 'deps = [{ package = "sqlite3", ref = "x" }]'), "'ref' without 'repo'"),
        (("[packages.sqlite3]\n", '[packages.sqlite3]\ndeps = [{ package = "proj" }]\n'), "cycle"),
        (('packages = ["proj"]\n', 'packages = ["proj"]\nartifact-prefix = "x"\n'), "both packages and artifact"),
        (('packages = ["proj"]\n', 'packages = ["stack-deps"]\n'), "publishes"),
        (("[packages.libaec]", "[packages.stack-deps]"), "repeats"),
    ],
)
def test_schema_rejects(patch: tuple[str, str], match: str) -> None:
    with pytest.raises(ManifestSchemaError, match=match):
        _schema(_STACK.replace(*patch, 1))


def test_a_meta_package_cannot_be_published() -> None:
    bundle = """
    [package]
    name = "bundle"
    repo = "o/bundle"
    compiler-inputs = []
    meta = true
    [[deps]]
    repo = "o/eckit"
    package = "eckit"
    ref = "2.3.0"
    [matrix.build]
    [[matrix.build.include]]
    platform = "p"
    """
    with pytest.raises(ManifestSchemaError, match="meta package"):
        _schema(bundle)
    assert _schema(bundle.replace("[matrix.build]\n", "[matrix.build]\n    publishes = false\n")).package.meta


# --- resolver -------------------------------------------------------------------------------------------------


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolve_deps, "resolve_ref_to_sha", lambda repo, ref, token: Sha("c" * 40))
    monkeypatch.setattr("ci_infrastructure.s3_store.object_exists", lambda name: True)


def _consumer_dep(package: str, compiler_inputs: list[str] | None = None) -> DepSpec:
    return DepSpec(
        repo=Repo("o/stack"),
        package=PackageName(package),
        ref=Ref("master"),
        compiler_inputs=compiler_inputs,
        build_type_input="build-type",
        platform_input="platform",
        needs_python=False,
        python_version_input="python-version",
    )


def _resolve(deps: list[DepSpec], cache: dict[tuple[Repo, Ref], Manifest]) -> tuple[Any, Any]:
    return resolve_leg(
        own=PackageSpec(
            name="eckit", prefix=PackageName("eckit"), repo=Repo("o/eckit"), compiler_inputs=["cxx-compiler"]
        ),
        own_deps=deps,
        own_sha=Sha("d" * 40),
        matrix_entry=dict(_LEG),
        manifest_cache=cache,
        sync_branch=None,
        sync_exists_by_repo={},
        sha_cache={},
        artifact_cache={},
        run_state_cache={},
        token=None,
        can_dispatch=False,
        lane=EXECUTION_RUNNER,
        dispatch_plans={},
    )


def _stack_cache() -> dict[tuple[Repo, Ref], Manifest]:
    return {(Repo("o/stack"), Ref("master")): parse_manifest(_STACK)}


@pytest.mark.usefixtures("offline")
def test_a_dep_on_one_library_fetches_only_it_and_its_siblings() -> None:
    deps, own = _resolve([_consumer_dep("proj")], _stack_cache())
    assert [d.name for d in deps] == ["sqlite3", "proj"]
    assert all(d.sha == "c" * 40 and d.ref == "master" for d in deps)
    assert own.direct_artifact_names == (deps[1].artifact_name,)


@pytest.mark.usefixtures("offline")
def test_compiler_inputs_come_from_the_producer_package_when_omitted() -> None:
    explicit, _ = _resolve([_consumer_dep("libaec", ["cxx-compiler"])], _stack_cache())
    implicit, _ = _resolve([_consumer_dep("libaec")], _stack_cache())
    assert [d.artifact_name for d in implicit] == [d.artifact_name for d in explicit]


@pytest.mark.usefixtures("offline")
def test_unknown_producer_package_without_compiler_inputs_is_an_error() -> None:
    with pytest.raises(ResolveError, match="declares no package 'nope'"):
        _resolve([_consumer_dep("nope")], _stack_cache())


@pytest.mark.usefixtures("offline")
def test_the_umbrella_stands_for_its_members() -> None:
    via_umbrella, own = _resolve([_consumer_dep("stack-deps")], _stack_cache())
    direct, own_direct = _resolve([_consumer_dep(p) for p in ("libaec", "sqlite3", "proj")], _stack_cache())
    assert sorted(d.name for d in via_umbrella) == ["libaec", "proj", "sqlite3"]
    assert sorted(own.direct_artifact_names) == sorted(own_direct.direct_artifact_names)
    assert own.deps_hash == own_direct.deps_hash


@pytest.mark.usefixtures("offline")
def test_an_umbrella_and_a_member_declared_together_agree() -> None:
    deps, _ = _resolve([_consumer_dep("stack-deps"), _consumer_dep("proj")], _stack_cache())
    assert sorted(d.name for d in deps) == ["libaec", "proj", "sqlite3"]


def test_an_artifact_prefix_keeps_the_repo_deps() -> None:
    m = parse_manifest(
        """
[package]
name = "a"
repo = "o/a"
compiler-inputs = []
[[deps]]
repo = "o/b"
package = "b"
ref = "main"
compiler-inputs = []
[matrix.secondary]
artifact-prefix = "a-extra"
[[matrix.secondary.include]]
platform = "p"
"""
    )
    assert m.packages_by_kind["secondary"] == ("a-extra",)
    assert [d.package for d in m.packages[PackageName("a-extra")].deps] == ["b"]


# --- generator ------------------------------------------------------------------------------------------------


_STACK_GENERATED: Final = (
    _STACK.replace('repo = "o/stack"', 'repo = "org/stack"')
    .replace("[matrix.libaec]\n", '[matrix.libaec]\ntriggers = ["rebuild-request"]\ndefaults.job-script = "./b.sh"\n')
    .replace("[matrix.sqlite3]\n", '[matrix.sqlite3]\ntriggers = ["rebuild-request"]\ndefaults.job-script = "./b.sh"\n')
    .replace("[matrix.proj]\n", '[matrix.proj]\ntriggers = ["rebuild-request"]\ndefaults.job-script = "./b.sh"\n')
    + '\n[[trigger-downstream]]\nrepo = "org/eckit"\nref = "main"\n'
)

_ECKIT: Final = """
[package]
name = "eckit"
repo = "org/eckit"
compiler-inputs = ["cxx-compiler"]
[[deps]]
repo = "org/stack"
package = "{package}"
ref = "master"
[matrix.build]
triggers = ["upstream-change"]
defaults.job-script = "./b.sh"
[[matrix.build.include]]
platform = "ubuntu-24.04"
cxx-compiler = "g++-13"
"""


def _needs(tmp_path: Path, package: str) -> dict[str, dict[str, list[str]]]:
    write_repo(tmp_path, "stack", _STACK_GENERATED)
    write_repo(tmp_path, "eckit", _ECKIT.format(package=package))
    manifests = parse_all(tmp_path)
    derive_cross_repo_needs(manifests)
    validate_graph(manifests)
    return {m.package_name: {k: list(mk.needs) for k, mk in m.matrices.items()} for m in manifests}


def test_a_package_waits_for_its_siblings_kind(tmp_path: Path) -> None:
    assert _needs(tmp_path, "proj")["stack-deps"] == {"libaec": [], "sqlite3": [], "proj": ["sqlite3"]}


def test_a_dep_on_one_library_needs_only_its_kind(tmp_path: Path) -> None:
    assert _needs(tmp_path, "proj")["eckit"] == {"build": ["stack-deps/proj"]}


def test_a_dep_on_the_umbrella_needs_every_member_kind(tmp_path: Path) -> None:
    needs = _needs(tmp_path, "stack-deps")["eckit"]["build"]
    assert sorted(needs) == ["stack-deps/libaec", "stack-deps/proj", "stack-deps/sqlite3"]


def test_one_kind_publishing_several_packages_is_not_supported_yet(tmp_path: Path) -> None:
    body = _STACK_GENERATED.replace('packages = ["proj"]', 'packages = ["proj", "libaec"]')
    write_repo(tmp_path, "stack", body)
    with pytest.raises(SchemaError, match="not supported yet"):
        parse_all(tmp_path)
