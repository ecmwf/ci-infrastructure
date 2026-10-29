# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from ci_infrastructure._github_api import ManifestSchemaError
from ci_infrastructure.manifest import (
    DepTable,
    DownstreamTable,
    GeneratedTable,
    ManifestFile,
    MatrixKindTable,
    PackageTable,
    ReleaseTable,
    TriggerDownstreamTable,
    validate,
)

_PACKAGE: dict[str, Any] = {"name": "a", "prefix": "a", "repo": "o/a", "compiler-inputs": [], "visibility": "private"}
_DEP: dict[str, Any] = {"repo": "o/b", "package": "b", "ref": "main", "compiler-inputs": []}


@pytest.mark.parametrize(
    "table",
    [PackageTable, DepTable, TriggerDownstreamTable, MatrixKindTable, GeneratedTable, DownstreamTable, ReleaseTable],
)
def test_every_key_is_described(table: type[BaseModel]) -> None:
    """The manifest reference is generated from these descriptions."""
    assert table.__doc__
    assert [name for name, f in table.model_fields.items() if not f.description] == []


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"package": _PACKAGE, "pakage": {}}, r"pakage"),
        ({"package": {**_PACKAGE, "prefx": "a"}}, r"\[package\]\.prefx"),
        ({"package": _PACKAGE, "deps": [{**_DEP, "reff": "x"}]}, r"\[\[deps\]\]\[0\]\.reff"),
        ({"package": _PACKAGE, "downstream": {"exclude": [], "excludes": []}}, r"\[downstream\]\.excludes"),
        ({"package": _PACKAGE, "downstream-gate": {"label": "run-downstream-ci"}}, r"downstream-gate"),
    ],
)
def test_unknown_keys_are_rejected(data: dict[str, Any], match: str) -> None:
    with pytest.raises(ManifestSchemaError, match=match):
        validate(data)


def test_json_schema_uses_toml_keys() -> None:
    props = ManifestFile.model_json_schema(by_alias=True)["$defs"]["DepTable"]["properties"]
    assert "compiler-inputs" in props
    assert "options-input" in props


def test_prefix_defaults_to_name() -> None:
    package = {k: v for k, v in _PACKAGE.items() if k != "prefix"}
    assert validate({"package": {**package, "name": "b"}}).package.prefix == "b"
    assert validate({"package": {**package, "prefix": "c"}}).package.prefix == "c"


def test_hpc_is_no_longer_an_execution() -> None:
    with pytest.raises(ValidationError):
        MatrixKindTable.model_validate({"execution": "hpc"})


def test_add_to_path_takes_dirs_inside_the_install_tree() -> None:
    assert PackageTable.model_validate({**_PACKAGE, "add-to-path": ["bin", "libexec/tools"]}).add_to_path == (
        "bin",
        "libexec/tools",
    )
    for bad in ["/usr/bin", "../bin", "bin:sbin", ""]:
        with pytest.raises(ValidationError, match="add-to-path"):
            PackageTable.model_validate({**_PACKAGE, "add-to-path": [bad]})


# --- [release] and release-ref (CD v3) -------------------------------------------------------------------------

_RELEASE_KIND: dict[str, Any] = {
    "packages": ["a-release"],
    "publishes": False,
    "include": [{"platform": "rocky-9", "channel": "rpm"}],
}


def _release_manifest(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "package": _PACKAGE,
        "deps": [{**_DEP, "release-ref": "1.2.3"}],
        "packages": {"a-release": {"compiler-inputs": []}},
        "release": {},
        "matrix": {"release-rpm": _RELEASE_KIND},
    }
    data.update(over)
    return data


def test_release_defaults() -> None:
    m = validate(_release_manifest())
    assert m.release is not None
    assert (m.release.package_name, m.release.tag_prefix, m.release.version_parts) == ("a", "", 3)
    assert m.deps[0].release_ref == "1.2.3"


def test_release_exclude() -> None:
    m = validate(_release_manifest(release={"exclude": ["lib64/pkgconfig/a_test_*.pc", "share/a/t[0-9]?"]}))
    assert m.release is not None
    assert m.release.exclude == ("lib64/pkgconfig/a_test_*.pc", "share/a/t[0-9]?")
    m = validate(_release_manifest())
    assert m.release is not None and m.release.exclude == ()


def test_release_ref_false_means_from_the_image() -> None:
    assert validate(_release_manifest(deps=[{**_DEP, "release-ref": False}])).deps[0].release_ref is False


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"release": {"tag-prefix": "release-"}}, r"\[release\]\.tag-prefix"),
        ({"release": {"version-parts": 2}}, r"\[release\]\.version-parts"),
        ({"release": {"package-name": "a b"}}, r"\[release\]\.package-name"),
        ({"release": {"summary": "two\nlines"}}, r"\[release\]\.summary"),
        ({"release": {"exclude": [""]}}, r"\[release\]\.exclude"),
        ({"release": {"exclude": ["/opt/ecmwf/lib/x.pc"]}}, r"\[release\]\.exclude"),
        ({"release": {"exclude": ["lib/../etc/x"]}}, r"\[release\]\.exclude"),
        ({"release": {"exclude": ["../x"]}}, r"\[release\]\.exclude"),
        ({"release": {"exclude": ["lib//x"]}}, r"\[release\]\.exclude"),
        ({"release": {"exclude": ["./lib/x"]}}, r"\[release\]\.exclude"),
        ({"release": {"exclude": ["lib/"]}}, r"\[release\]\.exclude"),
        ({"release": {"exclude": ["lib/$x"]}}, r"\[release\]\.exclude"),
        ({"release": {"exclude": ["lib/a;b"]}}, r"\[release\]\.exclude"),
        ({"release": {"exclude": ['lib/"x"']}}, r"\[release\]\.exclude"),
        ({"release": {"exclude": ["lib/**/x.pc"]}}, r"\[release\]\.exclude"),
        ({"release": {"exclude": ["lib\\\\x"]}}, r"\[release\]\.exclude"),
        ({"release": {"exclude": ["lib/x", "lib/x"]}}, r"\[release\]\.exclude"),
        ({"deps": [_DEP]}, r"\[\[deps\]\]\[0\] \(o/b\) needs release-ref"),
        ({"deps": [{**_DEP, "release-ref": True}]}, r"\[\[deps\]\]\[0\]\.release-ref must be a tag"),
        ({"deps": [{**_DEP, "release-ref": ""}]}, r"release-ref must be a tag"),
        ({"deps": [{"package": "a-release", "release-ref": "1.0.0"}]}, r"'release-ref' without 'repo'"),
        (
            {"packages": {"a-release": {"compiler-inputs": [], "deps": [_DEP]}}},
            r"builds \[packages\.a-release\], which declares deps",
        ),
        ({"matrix": {"release-rpm": {**_RELEASE_KIND, "publishes": True}}}, r"set publishes = false"),
        ({"matrix": {"release-rpm": {**_RELEASE_KIND, "triggers": ["upstream-change"]}}}, r"takes no triggers"),
        (
            {"matrix": {"release-rpm": {k: v for k, v in _RELEASE_KIND.items() if k != "packages"}}},
            r"list the packages it builds",
        ),
    ],
)
def test_release_rules(over: dict[str, Any], match: str) -> None:
    with pytest.raises(ManifestSchemaError, match=match):
        validate(_release_manifest(**over))


def test_release_ref_and_release_kinds_need_a_release_table() -> None:
    with pytest.raises(ManifestSchemaError, match=r"no \[release\] table"):
        validate({"package": _PACKAGE, "deps": [{**_DEP, "release-ref": "1.0.0"}]})
    without = {k: v for k, v in _release_manifest(deps=[_DEP]).items() if k != "release"}
    with pytest.raises(ManifestSchemaError, match="release kind but the manifest has no"):
        validate(without)


def test_release_ref_on_a_package_entry_is_refused() -> None:
    packages = {
        "a-release": {"compiler-inputs": []},
        "b": {"compiler-inputs": [], "deps": [{**_DEP, "release-ref": "1"}]},
    }
    with pytest.raises(ManifestSchemaError, match=r"\[packages\.b\] sets release-ref"):
        validate(_release_manifest(packages=packages))


def test_a_manifest_without_release_is_unchanged() -> None:
    m = validate({"package": _PACKAGE, "deps": [_DEP]})
    assert m.release is None and m.deps[0].release_ref is None
