# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""rust-bindings-{runner,publish}.sh.j2: Rust bindings checked against their deps' install trees, and published."""

from __future__ import annotations

import subprocess
from typing import Any, Final

from ci_infrastructure.hpc import jobscript

CARGO: Final = '{% extends "ci-infrastructure/rust-bindings-runner.sh.j2" %}\n'
PUBLISH: Final = '{% extends "ci-infrastructure/rust-bindings-publish.sh.j2" %}\n'
FEATURES: Final = "--workspace --no-default-features --features system"

LEG: Final[dict[str, Any]] = {
    "c-compiler": "gcc-13",
    "cxx-compiler": "g++-13",
    "rust-compiler": "rust-1.90",
    "platform": "ubuntu-24.04",
}


def _render(source: str = CARGO, leg: dict[str, Any] | None = None) -> str:
    return jobscript.render_job_template(
        template_source=source, template_name="rust.sh.j2", leg=leg or LEG, execution="runner"
    )


def test_cargo_checks_the_workspace_with_the_legs_toolchain() -> None:
    out = _render()
    assert "rust_version=1.90\n" in out
    assert 'export CC="$(command -v gcc-13)" CXX="$(command -v g++-13)"' in out
    assert 'cd "$CI_SOURCE_DIR/rust"' in out
    assert out.index("cargo fmt --all --check") < out.index(f'cargo test "${{cargo_patches[@]}}" {FEATURES}')
    assert "cmake" not in out


def test_a_recipe_sets_the_features_the_workspace_and_skips_the_docs() -> None:
    source = CARGO + (
        "{% block cargo_workspace %}bindings{% endblock %}\n"
        "{% block cargo_features %}--features ssl{% endblock %}\n"
        "{% block cargo_doc %}{% endblock %}\n"
    )
    out = _render(source)
    assert 'cd "$CI_SOURCE_DIR/bindings"' in out
    assert 'cargo clippy "${cargo_patches[@]}" --workspace --all-targets --features ssl -- -D warnings\n' in out
    assert 'cargo test "${cargo_patches[@]}" --workspace --features ssl\n' in out
    assert "cargo doc" not in out and "cargo publish" not in out


def test_a_leg_without_rust_compiler_is_refused() -> None:
    leg = {k: v for k, v in LEG.items() if k != "rust-compiler"}
    assert jobscript.undeclared_template_names(CARGO, leg, template_name="t") == {"rust_compiler"}
    assert jobscript.undeclared_template_names(CARGO, LEG, template_name="t") == set()


def test_the_rendered_script_is_valid_bash() -> None:
    subprocess.run(["bash", "-n"], input=_render(), text=True, check=True)


def test_the_listed_crates_follow_the_commit_their_repo_was_built_from() -> None:
    deps = [{"repo": "ecmwf/eckit", "ref": "develop", "sha": "a" * 40}, {"repo": "ecmwf/ecbuild", "sha": "b" * 40}]
    out = _render(leg={**LEG, "_resolved": {"deps": deps, "crates": {"eckit-sys": "ecmwf/eckit"}}})
    args = f"--dep ecmwf/ecbuild@{'b' * 40} --dep ecmwf/eckit@{'a' * 40} --crate eckit-sys=ecmwf/eckit"
    assert f'"$CI_INFRASTRUCTURE_PYTHON" -I -m ci_infrastructure.cargo_patches {args})"' in out
    assert f"-m ci_infrastructure.cargo_patches {args} --check\n" in out
    assert out.index(" --check\n") < out.index("cargo fmt")
    assert "cargo metadata" not in _render()
    subprocess.run(["bash", "-n"], input=out, text=True, check=True)


def test_deps_without_crates_still_check_cargo_toml() -> None:
    out = _render(leg={**LEG, "_resolved": {"deps": [{"repo": "ecmwf/eckit", "sha": "a" * 40}]}})
    assert "cargo metadata --no-deps" in out and 'cargo metadata "${cargo_patches[@]}"' not in out


def test_a_publish_leg_patches_nothing_and_builds_the_default_features() -> None:
    deps = [{"repo": "ecmwf/eckit", "sha": "a" * 40}]
    out = _render(PUBLISH, leg={**LEG, "_resolved": {"deps": deps, "crates": {"eckit-sys": "ecmwf/eckit"}}})
    assert 'case "${CI_CARGO_PUBLISH:-dry-run}" in' in out
    assert out.endswith('cargo publish "${dry_run[@]}" "${skip[@]}" --workspace \n')
    assert "cargo_patches" not in out and "cargo metadata" not in out and "cargo test" not in out
    assert jobscript.undeclared_template_names(PUBLISH, LEG, template_name="t") == set()
    subprocess.run(["bash", "-n"], input=out, text=True, check=True)


def test_a_publish_recipe_sets_the_features() -> None:
    out = _render(PUBLISH + "{% block cargo_features %}--features eckit-geo{% endblock %}\n")
    assert out.endswith('cargo publish "${dry_run[@]}" "${skip[@]}" --workspace --features eckit-geo\n')


def test_a_publish_leaves_out_the_named_crates_the_registry_has() -> None:
    out = _render(PUBLISH, leg={**LEG, "cargo-skip-published": ["foo-compat", "foo bar"]})
    assert "\"$CI_INFRASTRUCTURE_PYTHON\" -I -m ci_infrastructure.cargo_published foo-compat 'foo bar')" in out
    assert 'skip+=(--exclude "$crate")' in out
    subprocess.run(["bash", "-n"], input=out, text=True, check=True)
