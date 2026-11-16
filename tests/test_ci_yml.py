# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import parse_all, write_repo

from ci_infrastructure import check_ci_approval, check_ci_credentials, check_ci_result
from ci_infrastructure._github_api import ManifestSchemaError
from ci_infrastructure.ci_yml import ci_cron, render_ci_workflow
from ci_infrastructure.generate_downstream_ci import SchemaError, _run
from ci_infrastructure.manifest import validate

_BUILD = """
    [matrix.build]
    triggers = ["upstream-change", "rebuild-request"]
    defaults.job-script = "./.ci/build.sh.j2"
    [[matrix.build.include]]
    runs-on = "arc-runner-normal"
    platform = "linux"
"""


def _package(name: str, extra: str = "", deps: tuple[str, ...] = ()) -> str:
    body = f"""
    [package]
    name = "{name}"
    repo = "org/{name}"
    visibility = "public"
    compiler-inputs = []
    {extra}
    """
    for d in deps:
        body += f"""
    [[deps]]
    repo = "org/{d}"
    package = "{d}"
    ref = "main"
    """
    return body


def _graph(tmp_path: Path, **repos: tuple[str, tuple[str, ...]]) -> dict[str, Any]:
    for name, (extra, deps) in repos.items():
        write_repo(tmp_path, name, _package(name, extra, deps) + _BUILD)
    return {m.repo: m for m in parse_all(tmp_path)}


def _recipe(tmp_path: Path, name: str) -> None:
    (tmp_path / name / ".ci" / "build.sh.j2").write_text("true\n")


def _doc(tmp_path: Path, name: str, by_repo: dict[str, Any]) -> dict[Any, Any]:
    out = render_ci_workflow(by_repo[f"org/{name}"], by_repo)
    path = tmp_path / name / ".github" / "workflows" / "ci.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out)
    doc: dict[Any, Any] = yaml.safe_load(out)
    return doc


def test_a_schedule_follows_the_latest_dependency(tmp_path: Path) -> None:
    by_repo = _graph(
        tmp_path,
        a=('ci-schedule = "7 0 * * *"', ()),
        b=('ci-schedule = "50 0 * * *"', ()),
        off=("ci-schedule = false", ()),
        c=("", ("a", "b", "off")),
        d=('ci-schedule = "from-dependencies+40"', ("c",)),
    )
    assert ci_cron(by_repo["org/c"], by_repo) == "5 1 * * *"
    assert ci_cron(by_repo["org/d"], by_repo) == "45 1 * * *"
    assert ci_cron(by_repo["org/off"], by_repo) is None


@pytest.mark.parametrize(
    ("root", "extra", "message"),
    [
        ("ci-schedule = false", "", "no dependency has a schedule"),
        ('ci-schedule = "*/30 0 * * *"', "", "cannot derive ci-schedule"),
        ('ci-schedule = "50 23 * * 0"', "", "passes midnight on a schedule with days"),
    ],
)
def test_a_schedule_without_a_base_fails(tmp_path: Path, root: str, extra: str, message: str) -> None:
    by_repo = _graph(tmp_path, a=(root, ()), b=(extra, ("a",)))
    with pytest.raises(SchemaError, match=message):
        ci_cron(by_repo["org/b"], by_repo)


def test_a_repo_without_dependencies_names_its_schedule(tmp_path: Path) -> None:
    by_repo = _graph(tmp_path, a=("", ()))
    with pytest.raises(SchemaError, match=r"set \[package\].ci-schedule"):
        ci_cron(by_repo["org/a"], by_repo)


def test_a_daily_schedule_wraps_at_midnight(tmp_path: Path) -> None:
    by_repo = _graph(tmp_path, a=('ci-schedule = "50 23 * * *"', ()), b=("", ("a",)))
    assert ci_cron(by_repo["org/b"], by_repo) == "5 0 * * *"


@pytest.mark.parametrize("value", [True, "from-dependencies", "from-dependencies+x", "7 0 * *", "7 0 * * MON"])
def test_ci_schedule_takes_only_the_three_forms(value: Any) -> None:
    raw = {"package": {"name": "a", "repo": "o/a", "visibility": "public", "compiler-inputs": [], "ci-schedule": value}}
    with pytest.raises(ManifestSchemaError, match="ci-schedule"):
        validate(raw)


def test_the_generated_ci_yml_holds_only_the_reader_key_where_code_runs(tmp_path: Path) -> None:
    by_repo = _graph(tmp_path, a=('ci-schedule = "7 0 * * *"', ()), b=("", ("a",)))
    doc = _doc(tmp_path, "b", by_repo)
    workflow = tmp_path / "b" / ".github" / "workflows" / "ci.yml"
    assert check_ci_credentials.check(workflow) == []
    assert check_ci_approval.main([str(workflow)]) == 0
    assert check_ci_result.main([str(workflow)]) == 0
    assert doc["name"] == "CI"
    assert doc[True]["schedule"] == [{"cron": "22 0 * * *"}]
    jobs = doc["jobs"]
    assert list(jobs) == ["ci-approval", "pre-commit", "resolve", "rebuild-producers", "build", "ci-result"]
    [mint] = [s for s in jobs["resolve"]["steps"] if s.get("id") == "mint"]
    assert mint["with"]["app"] == "reader" and mint["with"]["repositories"] == "a,b"
    assert jobs["build"]["needs"] == ["ci-approval", "resolve", "rebuild-producers", "pre-commit"]
    assert "needs['rebuild-producers'].result != 'failure'" in jobs["build"]["if"]
    assert "rebuild-producers" not in jobs["ci-result"]["needs"]


def test_a_repo_without_dependencies_rebuilds_and_waits_for_nothing(tmp_path: Path) -> None:
    jobs = _doc(tmp_path, "a", _graph(tmp_path, a=('ci-schedule = "7 0 * * *"', ())))["jobs"]
    assert "rebuild-producers" not in jobs
    assert not any("wait-for-deps" in s.get("uses", "") for s in jobs["resolve"]["steps"])


def test_kinds_follow_the_kind_that_builds_their_package(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
        _package("a", 'ci-schedule = "7 0 * * *"')
        + _BUILD
        + """
    [packages.a-rust]
    compiler-inputs = []
    [[packages.a-rust.deps]]
    package = "a"
    [matrix.rust]
    packages = ["a-rust"]
    publishes = false
    defaults.job-script = "./.ci/rust.sh.j2"
    [[matrix.rust.include]]
    runs-on = "arc-runner-normal"
    platform = "linux"
    [matrix.rust-publish]
    packages = ["a-rust"]
    publishes = false
    ci = false
    defaults.job-script = "./.ci/rust.sh.j2"
    [[matrix.rust-publish.include]]
    runs-on = "arc-runner-normal"
    platform = "linux"
    """,
    )
    by_repo = {m.repo: m for m in parse_all(tmp_path)}
    jobs = _doc(tmp_path, "a", by_repo)["jobs"]
    assert jobs["rust"]["needs"] == ["ci-approval", "resolve", "build"]
    assert "rust-publish" not in jobs
    assert jobs["ci-result"]["needs"][-2:] == ["build", "rust"]


def test_hpc_kinds_wait_behind_the_hpc_gate(tmp_path: Path) -> None:
    hpc = """
    [matrix.build-hpc]
    execution = "hpc-atos"
    defaults.job-script = "./.ci/hpc/build.sh.j2"
    [[matrix.build-hpc.include]]
    runs-on = "hpc-submit"
    site = "hpc-batch"
    platform = "linux"
    """
    write_repo(tmp_path, "a", _package("a", 'ci-schedule = "7 0 * * *"') + _BUILD)
    write_repo(tmp_path, "b", _package("b", deps=("a",)) + _BUILD + hpc)
    jobs = _doc(tmp_path, "b", {m.repo: m for m in parse_all(tmp_path)})["jobs"]
    assert jobs["wait-build-hpc"]["needs"] == ["ci-approval", "resolve", "rebuild-producers", "hpc-gate"]
    assert "wait-build-hpc" in jobs["build-hpc"]["needs"]
    assert jobs["build-hpc"]["if"].endswith("&& needs['hpc-gate'].outputs.run == 'true' }}")
    assert jobs["collect-result-hpc"]["needs"] == ["ci-approval", "hpc-gate", "wait-build-hpc", "build-hpc"]
    assert "build-hpc" not in jobs["ci-result"]["needs"]


def test_the_package_keys_shape_the_workflow(tmp_path: Path) -> None:
    extra = """ci-schedule = false
    ci-pre-commit = false
    full-history = true
    fork-pull-requests = true
    ci-paths-ignore = ["docs/**"]"""
    doc = _doc(tmp_path, "a", _graph(tmp_path, a=(extra, ())))
    assert "schedule" not in doc[True]
    assert doc[True]["push"]["paths-ignore"] == ["docs/**"]
    assert "workflow_call" in doc[True]
    assert "pre-commit" not in doc["jobs"]
    assert doc["jobs"]["ci-approval"]["steps"][0]["with"] == {"check-workflows": "true"}
    checkout = doc["jobs"]["build"]["steps"][2]
    assert checkout["uses"] == "ecmwf/ci-infrastructure/actions/checkout-under-test@main"
    assert checkout["with"]["fetch-depth"] == 0


def test_a_kind_named_like_a_job_of_ci_yml_fails(tmp_path: Path) -> None:
    write_repo(
        tmp_path, "a", _package("a", 'ci-schedule = "7 0 * * *"') + _BUILD.replace("matrix.build", "matrix.resolve")
    )
    [m] = parse_all(tmp_path)
    with pytest.raises(SchemaError, match="clash with a job of ci.yml"):
        render_ci_workflow(m, {m.repo: m})


def test_a_custom_ci_yml_is_never_touched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_repo(tmp_path, "a", _package("a", 'ci-yml = "custom"') + _BUILD)
    _recipe(tmp_path, "a")
    ci = tmp_path / "a" / ".github" / "workflows" / "ci.yml"
    ci.parent.mkdir(parents=True)
    ci.write_text("name: CI\n")
    monkeypatch.chdir(tmp_path / "a")
    _run(".ci/manifest.toml", check=False, sibling_root=tmp_path)
    assert ci.read_text() == "name: CI\n"


def test_print_ci_yml_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_repo(tmp_path, "a", _package("a", 'ci-yml = "custom"\n    ci-schedule = "7 0 * * *"') + _BUILD)
    _recipe(tmp_path, "a")
    monkeypatch.chdir(tmp_path / "a")
    _run(".ci/manifest.toml", check=False, sibling_root=tmp_path, print_ci_yml=True)
    assert yaml.safe_load(capsys.readouterr().out)["name"] == "CI"
    assert not (tmp_path / "a" / ".github").exists()
