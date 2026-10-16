# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""rust-bindings-runner.sh.j2: Rust bindings checked against their deps' install trees."""

from __future__ import annotations

import subprocess
from typing import Any, Final

from ci_infrastructure.hpc import jobscript

CARGO: Final = '{% extends "ci-infrastructure/rust-bindings-runner.sh.j2" %}\n'
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


def test_the_crates_are_packaged_last_and_published_only_on_a_release() -> None:
    out = _render()
    # Neither runs unless CI_CARGO_PUBLISH asks: not every crate is ready to publish yet.
    dry_run = f'  cargo publish --dry-run "${{cargo_patches[@]}}" {FEATURES}\n'
    assert out.index("cargo doc") < out.index('case "${CI_CARGO_PUBLISH:-}" in') < out.index(dry_run)
    assert "\ndry-run)\n" in out and "\npublish)\n" in out
    assert f'cargo publish "${{skip[@]}}" {FEATURES}\n' in out
    assert "cargo_published" not in out


def test_a_release_leaves_out_the_named_crates_the_registry_has() -> None:
    out = _render(leg={**LEG, "cargo-skip-published": ["foo-compat", "foo bar"]})
    assert "\"$CI_INFRASTRUCTURE_PYTHON\" -I -m ci_infrastructure.cargo_published foo-compat 'foo bar')" in out
    assert 'skip+=(--exclude "$crate")' in out
    subprocess.run(["bash", "-n"], input=out, text=True, check=True)


def test_a_recipe_sets_the_features_the_workspace_and_skips_the_docs_and_the_package() -> None:
    source = CARGO + (
        "{% block cargo_workspace %}bindings{% endblock %}\n"
        "{% block cargo_features %}--features ssl{% endblock %}\n"
        "{% block cargo_doc %}{% endblock %}\n"
        "{% block cargo_package %}{% endblock %}\n"
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


def test_crates_follow_the_branch_their_repo_was_resolved_to() -> None:
    deps = [{"repo": "ecmwf/eckit", "ref": "sync-branch/x"}, {"repo": "ecmwf/ecbuild", "ref": "develop"}]
    out = _render(leg={**LEG, "_resolved": {"deps": deps}})
    refs = "--ref ecmwf/ecbuild=develop --ref ecmwf/eckit=sync-branch/x)"
    assert f'"$CI_INFRASTRUCTURE_PYTHON" -I -m ci_infrastructure.cargo_patches {refs}' in out
    assert "cargo_patches.py" not in out and "cargo metadata" not in _render()
    subprocess.run(["bash", "-n"], input=out, text=True, check=True)
