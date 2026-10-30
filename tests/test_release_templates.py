# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The release overlay templates (templates/release/), and the install-tree checks they share."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import textwrap
from importlib import resources
from pathlib import Path
from typing import Any, Final

import pytest
from release_fixtures import HPC_LEG, RECIPE, REPO_ROOT, RPM_LEG, write_release_repo

from ci_infrastructure._github_api import Execution
from ci_infrastructure.hpc import jobscript, orchestrate
from ci_infrastructure.release import legs
from ci_infrastructure.release.detect import ReleaseEvent
from ci_infrastructure.release.version import parse_tag

SHA = "0123456789abcdef0123456789abcdef01234567"
CHECKS: Final = resources.files("ci_infrastructure") / "templates" / "release" / "_checks.sh.j2"

#: A CI leg of the same recipe; its rendering must not change with the overlay in place.
_CI_LEG: Final = {
    "build-type": "Release",
    "platform": "rocky-9",
    "c-compiler": "gcc-11",
    "cxx-compiler": "g++-11",
    "c-flags": "-fno-fast-math",
    "options": "release",
    "modules": ["load cmake"],
}
_CI_RECIPE: Final = (
    '{% extends "ci-infrastructure/cmake-all-lanes.sh.j2" %}\n'
    "{% block cmake_args %}\n-DCMAKE_C_FLAGS={{ c_flags | quote }} \\\n{% endblock %}\n"
)

#: Rendered by the code before the overlay existed (bdaf7cf).
GOLDEN_RUNNER: Final = (
    "#!/bin/bash\n"
    "\n"
    'build="${CI_BUILD_DIR:-${TMPDIR:-/tmp}/build}"\n'
    'jobs="$(nproc 2>/dev/null || sysctl -n hw.ncpu)"\n'
    "# CMake's default generator, as cmake-build used: some images ship a ninja too old for every project.\n"
    'gen_flag=""\n'
    "\n"
    'echo "Using: $(command -v gcc-11) ($(gcc-11 --version | head -1))"\n'
    'echo "Using: $(command -v g++-11) ($(g++-11 --version | head -1))"\n'
    'echo "Using: $(command -v cmake) ($(cmake --version | head -1))"\n'
    "\n"
    "# shellcheck disable=SC2086\n"
    'cmake --preset release -S "$CI_SOURCE_DIR" -B "$build" $gen_flag \\\n'
    "  -DCMAKE_BUILD_TYPE=Release \\\n"
    '  -DCMAKE_C_COMPILER="$(command -v gcc-11)" \\\n'
    '  -DCMAKE_CXX_COMPILER="$(command -v g++-11)" \\\n'
    "-DCMAKE_C_FLAGS=-fno-fast-math \\\n"
    "  -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=ON \\\n"
    '  -DCMAKE_PREFIX_PATH="$CMAKE_PREFIX_PATH" \\\n'
    '  -DCMAKE_INSTALL_PREFIX="$CI_INSTALL_PREFIX"\n'
    "\n"
    'cmake --build "$build" --parallel "$jobs"\n'
    "\n"
    'ctest --test-dir "$build" --output-on-failure -j "$jobs"\n'
    "\n"
    'cmake --install "$build"\n'
)
GOLDEN_HPC: Final = (
    "#!/bin/bash\n"
    "#SBATCH --qos=nf\n"
    "#SBATCH --nodes=1\n"
    "#SBATCH --ntasks=8\n"
    "#SBATCH --mem=64GB\n"
    "#SBATCH --gres=ssdtmp:30G\n"
    "#SBATCH --time=01:00:00\n"
    "\n"
    "module load cmake\n"
    "\n"
    'build="${TMPDIR:-/tmp}/build"\n'
    'jobs="${SLURM_CPUS_PER_TASK:-${SLURM_NTASKS:-8}}"\n'
    'install_root="$CI_INSTALL_PREFIX"\n'
    'gen_flag=""\n'
    'if command -v ninja >/dev/null 2>&1; then gen_flag="-GNinja"; fi\n'
    "\n"
    'echo "Using: $(command -v gcc-11) ($(gcc-11 --version | head -1))"\n'
    'echo "Using: $(command -v g++-11) ($(g++-11 --version | head -1))"\n'
    'echo "Using: $(command -v cmake) ($(cmake --version | head -1))"\n'
    "\n"
    "# shellcheck disable=SC2086\n"
    'cmake --preset release -S "$CI_SOURCE_DIR" -B "$build" $gen_flag \\\n'
    "  -DCMAKE_BUILD_TYPE=Release \\\n"
    '  -DCMAKE_C_COMPILER="$(command -v gcc-11)" \\\n'
    '  -DCMAKE_CXX_COMPILER="$(command -v g++-11)" \\\n'
    "-DCMAKE_C_FLAGS=-fno-fast-math \\\n"
    "  -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=ON \\\n"
    '  -DCMAKE_PREFIX_PATH="$CMAKE_PREFIX_PATH" \\\n'
    '  -DCMAKE_INSTALL_PREFIX="$CI_INSTALL_PREFIX"\n'
    "\n"
    'cmake --build "$build" --parallel "$jobs"\n'
    "\n"
    'ctest --test-dir "$build" --output-on-failure -j "$jobs"\n'
    "\n"
    'cmake --install "$build"\n'
    "\n"
    "# The fetcher takes the artifact from CI_INSTALL_ARCHIVE, not from the install\n"
    "# tree; .part + mv so it only ever appears complete.\n"
    'mkdir -p "$(dirname "$CI_INSTALL_ARCHIVE")"\n'
    'tar -cf - -C "$install_root" . | zstd -T0 -q -o "$CI_INSTALL_ARCHIVE.part"\n'
    'mv "$CI_INSTALL_ARCHIVE.part" "$CI_INSTALL_ARCHIVE"\n'
)


@pytest.mark.parametrize(("lane", "golden"), [("runner", GOLDEN_RUNNER), ("hpc-atos", GOLDEN_HPC)])
def test_a_ci_leg_renders_byte_identically(lane: str, golden: str) -> None:
    out = jobscript.render_job_template(
        template_source=_CI_RECIPE,
        template_name="build.sh.j2",
        leg=_CI_LEG,
        execution=lane,  # type: ignore[arg-type]
    )
    assert out == golden
    assert jobscript.RELEASE_OVERLAY_MARKER not in out


EXCLUDE: Final = 'exclude = ["lib64/pkgconfig/pkg_test_*.pc"]\n'


def _release_leg(tmp_path: Path, tag: str = "2.5.0-rc1", release_extra: str = "") -> dict[str, Any]:
    rm = legs.load(write_release_repo(tmp_path, release_extra=release_extra))
    event = ReleaseEvent(version=parse_tag(tag), sha=SHA, head_sha=SHA)
    [leg] = legs.expand(rm, ["release-rpm"], event)["release-rpm"]
    return leg


def _render(leg: dict[str, Any], source: str = RECIPE, execution: str = "runner") -> str:
    return jobscript.render_job_template(
        template_source=source,
        template_name="build.sh.j2",
        leg=leg,
        execution=execution,  # type: ignore[arg-type]
    )


def test_release_leg_renders_the_overlay(tmp_path: Path) -> None:
    out = _render(_release_leg(tmp_path))
    lines = out.splitlines()
    assert jobscript.RELEASE_OVERLAY_MARKER in lines
    # The recipe's cmake_args, re-declared inside the overlay's configure, still land.
    assert "-DCMAKE_C_FLAGS=-fno-fast-math \\" in lines
    assert "-DCMAKE_CXX_FLAGS=-fno-fast-math \\" in lines
    assert 'cmake -C "$release_overrides" --preset release -S "$CI_SOURCE_DIR" -B "$build" $gen_flag \\' in lines
    assert '  -DCMAKE_INSTALL_PREFIX="$release_prefix"' in lines
    assert "release_prefix=/opt/ecmwf" in lines
    assert "USE_LINK_PATH=ON" not in out
    assert 'DESTDIR="$build/stage" cmake --install "$build"' in lines
    assert 'cpack -G RPM --config "$build/CPackConfig.cmake" -B "$build/_cpack"' in lines
    # The blocks only the overlay declares render: they sit inside blocks the base defines.
    assert "  dnf -y install rpm-build" in lines  # packaging_tools, inside setup
    assert '} > "$CI_INSTALL_PREFIX/build-meta.json"' in lines  # build_meta, inside publish
    assert "for _ci_var in SCCACHE_BUCKET RUSTC_WRAPPER" in out  # release_guard, inside setup
    # The CPack cache and build metadata are rendered here, not computed by the job.
    assert 'set(CPACK_PACKAGE_VERSION "2.5.0~rc1" CACHE STRING "" FORCE)' in lines
    assert '  "job_key": "pkg-2.5.0-rc1-rpm-rocky-9-gxx-11-01234567",' in lines
    assert "CI_INFRASTRUCTURE_PYTHON" not in out
    assert 'ctest --test-dir "$build" --output-on-failure -j "$jobs"' in lines


@pytest.mark.parametrize("release_extra", ["", EXCLUDE], ids=["plain", "exclude"])
def test_rendered_release_script_is_valid_bash(tmp_path: Path, release_extra: str) -> None:
    script = tmp_path / "job.sh"
    script.write_text(_render(_release_leg(tmp_path, release_extra=release_extra)))
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_release_exclude_is_rendered_into_the_script(tmp_path: Path) -> None:
    assert "CPACK_PRE_BUILD_SCRIPTS" not in _render(_release_leg(tmp_path))
    leg = _release_leg(tmp_path, release_extra=EXCLUDE)
    out = _render(leg)
    lines = out.splitlines()
    assert "cat > \"$release_exclude\" <<'CI_RELEASE_EXCLUDE'" in lines
    assert 'foreach(_ci_glob IN ITEMS "lib64/pkgconfig/pkg_test_*.pc")' in lines
    assert 'set(_ci_root "${CPACK_TEMPORARY_DIRECTORY}/opt/ecmwf")' in lines
    assert '  -D "CPACK_PRE_BUILD_SCRIPTS=$release_exclude"' in lines
    assert 'if [ ! -f "$build/ci-release-exclude.done" ]; then' in lines
    assert "CI_INFRASTRUCTURE_PYTHON" not in out
    assert leg["_release"]["exclude"] == ["lib64/pkgconfig/pkg_test_*.pc"]
    assert '  "exclude": [' in lines  # in build-meta.json


def test_a_recipe_that_bypasses_the_overlay_is_refused(tmp_path: Path) -> None:
    leg = _release_leg(tmp_path)
    with pytest.raises(jobscript.JobTemplateError, match="without the release overlay"):
        _render(leg, '{% extends "ci-infrastructure/cmake-runner.sh.j2" %}\n')


_EXTENDS: Final = '{% extends "ci-infrastructure/cmake-all-lanes.sh.j2" %}\n'
ECKIT_CD_TEST_RECIPE: Final = REPO_ROOT.parent / "eckit-cd-test" / ".ci" / "build.sh.j2"


def _lane_leg(tmp_path: Path, lane: str) -> dict[str, Any]:
    return _hpc_leg(tmp_path) if lane == "hpc-atos" else _release_leg(tmp_path)


def test_the_release_owned_blocks_are_what_the_overlay_defines() -> None:
    assert jobscript.release_owned_blocks() == {
        "setup",
        "release_guard",
        "preflight",
        "configure",
        "install",
        "release_exclude",
        "publish",
        "build_meta",
        "verify",
    }
    assert not jobscript.release_owned_blocks() & jobscript.RELEASE_RECIPE_BLOCKS


@pytest.mark.parametrize("lane", ["runner", "hpc-atos"])
@pytest.mark.parametrize("block", ["verify", "publish", "install", "setup", "preflight", "configure", "build_meta"])
def test_a_recipe_overriding_a_release_owned_block_is_refused(tmp_path: Path, lane: str, block: str) -> None:
    """Even with super(): what the overlay does there is not the recipe's to change in a release."""
    recipe = RECIPE + f"{{% block {block} %}}{{{{ super() }}}}\necho skipped{{% endblock %}}\n"
    with pytest.raises(jobscript.JobTemplateError, match=rf"release-owned block\(s\) '{block}' in build.sh.j2"):
        _render(_lane_leg(tmp_path, lane), recipe, execution=lane)


@pytest.mark.parametrize("lane", ["runner", "hpc-atos"])
def test_a_release_owned_block_nested_in_an_allowed_one_is_refused(tmp_path: Path, lane: str) -> None:
    recipe = _EXTENDS + "{% block cmake_args %}{% block verify %}{% endblock %}{% endblock %}\n"
    with pytest.raises(jobscript.JobTemplateError, match="'verify'"):
        _render(_lane_leg(tmp_path, lane), recipe, execution=lane)


@pytest.mark.parametrize("lane", ["runner", "hpc-atos"])
def test_a_release_owned_block_in_a_repo_base_template_is_refused(tmp_path: Path, lane: str) -> None:
    """The repo's whole extends chain is checked, up to ci-infrastructure's own templates."""
    repo = tmp_path / "ci"
    repo.mkdir()
    (repo / "base.sh.j2").write_text(_EXTENDS + "{% block publish %}{% endblock %}\n")
    with pytest.raises(jobscript.JobTemplateError, match="'publish' in base.sh.j2"):
        jobscript.render_job_template(
            template_source='{% extends "base.sh.j2" %}\n',
            template_name="build.sh.j2",
            leg=_lane_leg(tmp_path, lane),
            execution=lane,  # type: ignore[arg-type]
            search_path=repo,
        )


_OVERRIDING: Final = (
    _EXTENDS
    + "{% block verify %}echo SKIPPED-VERIFY{% endblock %}\n"
    + "{% block install %}echo SKIPPED-INSTALL{% endblock %}\n"
)
_LANES: Final[tuple[Execution, ...]] = ("runner", "hpc-atos")


def _render_in_repo(tmp_path: Path, lane: Execution, recipe: str, repo: Path) -> str:
    return jobscript.render_job_template(
        template_source=recipe,
        template_name="build.sh.j2",
        leg=_lane_leg(tmp_path, lane),
        execution=lane,
        search_path=repo,
    )


@pytest.mark.parametrize("lane", _LANES)
@pytest.mark.parametrize(
    "recipe",
    [
        '{% include "evil.sh.j2" %}\n',
        _EXTENDS + '{% block cmake_args %}{% include "evil.sh.j2" %}{% endblock %}\n',
        '{% import "evil.sh.j2" as evil %}' + _EXTENDS,
        '{% from "evil.sh.j2" import x %}' + _EXTENDS,
    ],
    ids=["top-level-include", "include-in-cmake_args", "import", "from-import"],
)
def test_a_release_owned_block_in_an_included_repo_template_is_refused(
    tmp_path: Path, lane: Execution, recipe: str
) -> None:
    """An included template that extends ci-infrastructure's renders its own overrides: a bypass otherwise."""
    repo = tmp_path / "ci"
    repo.mkdir()
    (repo / "evil.sh.j2").write_text(_OVERRIDING)
    with pytest.raises(jobscript.JobTemplateError, match="'install' in evil.sh.j2; 'verify' in evil.sh.j2"):
        _render_in_repo(tmp_path, lane, recipe, repo)


@pytest.mark.parametrize("lane", _LANES)
def test_a_repo_template_under_the_base_prefix_is_still_checked(tmp_path: Path, lane: Execution) -> None:
    """A repo file at ci-infrastructure/<name> that the shared templates lack is the repo's, not exempt."""
    repo = tmp_path / "ci"
    (repo / "ci-infrastructure").mkdir(parents=True)
    (repo / "ci-infrastructure" / "evil.sh.j2").write_text(_OVERRIDING)
    with pytest.raises(jobscript.JobTemplateError, match="'verify' in ci-infrastructure/evil.sh.j2"):
        _render_in_repo(tmp_path, lane, '{% include "ci-infrastructure/evil.sh.j2" %}\n', repo)


@pytest.mark.parametrize("lane", _LANES)
def test_a_computed_template_name_is_refused_in_a_release(tmp_path: Path, lane: Execution) -> None:
    recipe = _EXTENDS + "{% block cmake_args %}{% include name_of_it %}{% endblock %}\n"
    with pytest.raises(jobscript.JobTemplateError, match="computed expression"):
        _render_in_repo(tmp_path, lane, recipe, tmp_path)


@pytest.mark.parametrize("lane", _LANES)
def test_an_included_repo_snippet_without_owned_blocks_still_renders(tmp_path: Path, lane: Execution) -> None:
    repo = tmp_path / "ci"
    repo.mkdir()
    (repo / "flags.j2").write_text("-DFROM_SNIPPET=ON \\\n")
    recipe = _EXTENDS + '{% block cmake_args %}{% include "flags.j2" %}{% endblock %}\n'
    assert "-DFROM_SNIPPET=ON \\" in _render_in_repo(tmp_path, lane, recipe, repo).splitlines()


@pytest.mark.parametrize("lane", ["runner", "hpc-atos"])
def test_the_allowed_blocks_and_ci_only_blocks_still_render(tmp_path: Path, lane: str) -> None:
    recipe = RECIPE + "{% block packaging_tools %}echo own-rpmbuild{% endblock %}\n"
    recipe += "{% block set_environment %}echo own-env{% endblock %}\n"
    out = _render(_lane_leg(tmp_path, lane), recipe, execution=lane)
    assert "echo own-env" in out
    assert ("echo own-rpmbuild" in out) == (lane == "runner")  # packaging_tools is the runner overlay's


def test_a_ci_leg_may_still_override_any_block() -> None:
    recipe = _EXTENDS + "{% block install %}echo own-install{% endblock %}\n"
    out = jobscript.render_job_template(
        template_source=recipe, template_name="build.sh.j2", leg=_CI_LEG, execution="runner"
    )
    assert "echo own-install" in out


@pytest.mark.skipif(not ECKIT_CD_TEST_RECIPE.is_file(), reason="eckit-cd-test is not checked out beside this repo")
@pytest.mark.parametrize("lane", ["runner", "hpc-atos"])
def test_eckit_cd_tests_recipe_passes(tmp_path: Path, lane: str) -> None:
    out = _render(_lane_leg(tmp_path, lane), ECKIT_CD_TEST_RECIPE.read_text(), execution=lane)
    assert jobscript.RELEASE_OVERLAY_MARKER in out.splitlines()


def test_an_rpm_leg_renders_nothing_on_the_hpc_lane(tmp_path: Path) -> None:
    with pytest.raises(jobscript.JobTemplateError, match="renders nothing"):
        _render(_release_leg(tmp_path), execution="hpc-atos")


def test_an_hpc_leg_renders_nothing_on_a_runner(tmp_path: Path) -> None:
    with pytest.raises(jobscript.JobTemplateError, match="renders nothing"):
        _render(_hpc_leg(tmp_path), execution="runner")


# --- the HPC (hpc-module) overlay -----------------------------------------------------------------------------


def _hpc_leg(tmp_path: Path, tag: str = "2.5.0-rc1", release_extra: str = "") -> dict[str, Any]:
    rm = legs.load(write_release_repo(tmp_path, leg=RPM_LEG + HPC_LEG, release_extra=release_extra))
    event = ReleaseEvent(version=parse_tag(tag), sha=SHA, head_sha=SHA)
    [leg] = legs.expand(rm, ["release-hpc"], event)["release-hpc"]
    return leg


def test_hpc_release_leg_renders_the_overlay_and_its_install_test(tmp_path: Path) -> None:
    out = _render(_hpc_leg(tmp_path, release_extra=EXCLUDE), execution="hpc-atos")
    lines = out.splitlines()
    assert jobscript.RELEASE_OVERLAY_MARKER in lines
    # The atos base still frames it: #SBATCH header and the leg's modules.
    assert "#SBATCH --time=01:00:00" in lines and "module load gcc/old" in lines
    assert 'release_prefix="$SCRATCH"/cd-v3-dry-run/pkg/2.5.0-rc1/GNU/8.5' in lines
    assert "-DCMAKE_C_FLAGS=-fno-fast-math \\" in lines  # the recipe's cmake_args, re-declared
    assert '  -DCMAKE_INSTALL_PREFIX="$release_prefix"' in lines
    assert "USE_LINK_PATH" not in out
    assert 'ctest --test-dir "$build" --output-on-failure -j "$jobs"' in lines
    assert 'DESTDIR="$install_root" cmake --install "$build"' in lines
    # The blocks only the overlay declares render: they sit inside blocks the base defines.
    assert "for _ci_var in SCCACHE_BUCKET RUSTC_WRAPPER" in out  # release_guard, inside setup
    assert "_ci_globs=( 'lib64/pkgconfig/pkg_test_*.pc' )" in lines  # release_exclude, inside install
    assert '} > "$CI_INSTALL_ARCHIVE.build-meta.json"' in lines  # build_meta, inside publish
    assert '} > "$CI_INSTALL_ARCHIVE.itest.json"' in lines  # verify, inside publish
    assert '  "job_key": "pkg-2.5.0-rc1-hpc-hpc-atos-gnu-gxx-8-01234567",' in lines
    # The install test: build gone, modules purged and reloaded, no LD_LIBRARY_PATH, the checks.
    verify = out[out.index("# --- Install test") :]
    for needle in (
        'rm -rf "$build" "$install_root"',
        "module purge",
        "module load gcc/old",
        "module list 2>&1",
        "unset LD_LIBRARY_PATH",
        'ci_release_check_runpath "$ci_itest_tree" "${ci_release_allowed[@]}"',
        'ci_release_check_ldd "$ci_itest_tree"',
        "ci_release_check_leaks",
        "  bin/pkg-version",
    ):
        assert needle in verify, needle
    assert verify.index("module purge") < verify.index("unset LD_LIBRARY_PATH") < verify.index("bin/pkg-version")
    assert "ci_release_check_runpath() {" in out  # _checks.sh.j2, included
    # Nothing the compute node does not have.
    assert "CI_INFRASTRUCTURE_PYTHON" not in out and "python" not in out.lower()


@pytest.mark.parametrize("release_extra", ["", EXCLUDE], ids=["plain", "exclude"])
def test_rendered_hpc_release_script_is_valid_bash(tmp_path: Path, release_extra: str) -> None:
    from ci_infrastructure.render_job import wrap

    leg = _hpc_leg(tmp_path, release_extra=release_extra)
    script = tmp_path / "job.sh"
    script.write_text(wrap(_render(leg, execution="hpc-atos"), leg=leg, execution="hpc-atos"))
    subprocess.run(["bash", "-n", str(script)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(script)], check=True)


def test_the_hpc_lane_base_templates_are_unchanged() -> None:
    """The HPC overlay extends cmake-atos.sh.j2; it never edits it (HPC_TEMPLATE_VERSION pins its digest)."""
    overlay = (resources.files("ci_infrastructure") / "templates" / "release" / "cmake-atos-release.sh.j2").read_text()
    assert overlay.startswith('{% extends "ci-infrastructure/cmake-atos.sh.j2" %}')


_HPC_STUBS: Final = {
    "module": 'case "$1" in list) echo "Currently Loaded Modules: gcc/8.5.0 cmake" >&2 ;; esac',
    "gcc": 'echo "gcc (GCC) 8.5.0"',
    "g++": 'echo "g++ (GCC) 8.5.0"',
    "ctest": "exit 0",
    # Installs a tree as the project would: a smoke program, a .pc, a test-only .pc to exclude,
    # and with STUB_LEAK a CMake file naming the node-local source dir.
    "cmake": textwrap.dedent(
        """\
        case "$1" in
          --version) echo "cmake version 3.31.0" ;;
          --preset|--build) ;;
          --install)
            t="$DESTDIR$STUB_PREFIX"
            mkdir -p "$t/bin" "$t/lib64/pkgconfig" "$t/lib64/cmake/pkg"
            printf '#!/bin/bash\\n[ -z "${STUB_SMOKE_FAIL:-}" ] && echo "pkg 2.5.0 in $PWD"\\n' > "$t/bin/pkg-version"
            chmod +x "$t/bin/pkg-version"
            echo "prefix=$STUB_PREFIX" > "$t/lib64/pkgconfig/pkg.pc"
            echo "prefix=/nowhere" > "$t/lib64/pkgconfig/pkg_test_custom.pc"
            if [ -n "${STUB_LEAK:-}" ]; then echo "set(X $CI_SOURCE_DIR/src)" > "$t/lib64/cmake/pkg/x.cmake"; fi
            ;;
          *) exit 3 ;;
        esac
        """
    ),
    # No zstd here: a "compressed" archive is the plain tar.
    "zstd": textwrap.dedent(
        """\
        out=""; dc=0; file=""
        while [ $# -gt 0 ]; do
          case "$1" in -o) out="$2"; shift ;; -dc) dc=1 ;; -T0|-q) ;; *) file="$1" ;; esac
          shift
        done
        if [ "$dc" = 1 ]; then cat "$file"; else cat > "$out"; fi
        """
    ),
}


def _run_hpc_stubbed(tmp_path: Path, release_extra: str = EXCLUDE, **env: str) -> subprocess.CompletedProcess[str]:
    from ci_infrastructure.render_job import wrap

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in _HPC_STUBS.items():
        stub = bin_dir / name
        stub.write_text("#!/bin/bash\n" + body)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    leg = _hpc_leg(tmp_path, release_extra=release_extra)
    script = tmp_path / "job.sh"
    script.write_text(wrap(_render(leg, execution="hpc-atos"), leg=leg, execution="hpc-atos"))
    scratch = tmp_path / "scratch"
    (tmp_path / "tmp").mkdir(exist_ok=True)
    run_env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "SCRATCH": str(scratch),
        "STUB_PREFIX": f"{scratch}/cd-v3-dry-run/pkg/2.5.0-rc1/GNU/8.5",
        "TMPDIR": str(tmp_path / "tmp"),
        "SLURM_JOB_ID": "42",
        "CI_SOURCE_DIR": str(tmp_path / "tmp" / "ci-src-1"),
        "CI_INSTALL_PREFIX": str(tmp_path / "work" / "install" / "key"),
        "LD_LIBRARY_PATH": "/from/the/job/env",
        **env,
    }
    return subprocess.run(["bash", str(script)], cwd=tmp_path, env=run_env, capture_output=True, text=True)


def test_the_hpc_script_archives_the_prefix_and_tests_it(tmp_path: Path) -> None:
    result = _run_hpc_stubbed(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    archive = tmp_path / "work" / "install" / "key.install.tar.zst"
    names = subprocess.run(["tar", "-tf", str(archive)], capture_output=True, text=True, check=True).stdout.split()
    # Relative to the prefix, and without the excluded file.
    assert "./bin/pkg-version" in names and "./lib64/pkgconfig/pkg.pc" in names
    assert not [n for n in names if "pkg_test_" in n]
    assert "removing" in result.stdout and "pkg_test_custom.pc" in result.stdout
    digest = subprocess.run(["sha256sum", str(archive)], capture_output=True, text=True).stdout.split()[0]
    assert Path(f"{archive}.sha256").read_text().strip() == digest
    itest = json.loads(Path(f"{archive}.itest.json").read_text())
    assert itest["result"] == "passed" and itest["sha256"] == digest
    assert (itest["job_key"], itest["sha"]) == ("pkg-2.5.0-rc1-hpc-hpc-atos-gnu-gxx-8-01234567", SHA)
    assert itest["prefix"] == f"{tmp_path}/scratch/cd-v3-dry-run/pkg/2.5.0-rc1/GNU/8.5"
    meta = json.loads(Path(f"{archive}.build-meta.json").read_text())
    assert meta["release"]["job_key"] == itest["job_key"] and meta["sha256"] == digest
    assert "gcc/8.5.0" in Path(f"{archive}.modules").read_text()
    # The smoke command ran in the unpacked prefix; the build and staging trees are gone.
    assert "pkg 2.5.0 in " in result.stdout and "/ci-itest-42" in result.stdout
    assert not (tmp_path / "work" / "install" / "key").exists()
    assert not (tmp_path / "tmp" / "ci-itest-42").exists()


def test_the_hpc_script_starts_from_a_clean_install_dir(tmp_path: Path) -> None:
    stale = tmp_path / "work" / "install" / "key" / "scratch" / "old"
    stale.mkdir(parents=True)
    (tmp_path / "work" / "install" / "key.install.tar.zst.itest.json").write_text("{}")
    result = _run_hpc_stubbed(tmp_path)
    assert result.returncode == 0, result.stderr
    names = subprocess.run(
        ["tar", "-tf", str(tmp_path / "work" / "install" / "key.install.tar.zst")], capture_output=True, text=True
    ).stdout
    assert "old" not in names
    assert json.loads((tmp_path / "work" / "install" / "key.install.tar.zst.itest.json").read_text())["result"]


@pytest.mark.parametrize(
    ("env", "release_extra", "message"),
    [
        ({"STUB_SMOKE_FAIL": "1"}, EXCLUDE, ""),
        ({"STUB_LEAK": "1"}, EXCLUDE, "build paths leaked"),
        ({}, 'exclude = ["lib/none_*.pc"]\n', "matched nothing"),
        ({"SCCACHE_BUCKET": "b"}, EXCLUDE, "SCCACHE_BUCKET is set"),
    ],
    ids=["smoke", "leak", "stale-exclude", "sccache"],
)
def test_the_hpc_script_fails_without_an_install_test_receipt(
    tmp_path: Path, env: dict[str, str], release_extra: str, message: str
) -> None:
    result = _run_hpc_stubbed(tmp_path, release_extra=release_extra, **env)
    assert result.returncode != 0
    assert message in result.stderr
    assert not (tmp_path / "work" / "install" / "key.install.tar.zst.itest.json").exists()


def test_a_leg_field_named_release_is_refused() -> None:
    with pytest.raises(jobscript.JobTemplateError, match="'release'"):
        jobscript.build_template_context({**_CI_LEG, "release": "x"}, execution="runner")


def test_release_must_be_a_table() -> None:
    # Every release leg sets install-test-command, which the overlay of each lane reads.
    leg = {**_CI_LEG, "install-test-command": "bin/x", "_release": "x"}
    with pytest.raises(jobscript.JobTemplateError, match="must be a table"):
        jobscript.render_job_template(template_source=_CI_RECIPE, template_name="b.sh.j2", leg=leg, execution="runner")


def test_static_check_follows_the_overlay(tmp_path: Path) -> None:
    leg = _release_leg(tmp_path)
    assert jobscript.undeclared_template_names(RECIPE, leg, template_name="build.sh.j2") == set()
    assert "release" in jobscript.declared_template_names(leg)


def test_release_legs_without_resolved_suit_the_hpc_helpers(tmp_path: Path) -> None:
    """Gate legs carry no `_resolved`; the code that reads it must cope."""
    leg = _release_leg(tmp_path)
    as_json = json.dumps(leg)
    assert orchestrate.batch_packages(as_json) == {}
    assert orchestrate.deps_path_dirs(as_json, {}) == []
    ctx = jobscript.build_template_context(leg, execution="runner")
    assert ctx["ci_packages"] == [] and ctx["ci_dep_shas"] == {}
    assert ctx["release"] == leg["_release"]
    assert "_release" not in ctx and "_resolved" not in ctx


# --- the rendered script, run against stub tools ----------------------------------------------------------

_STUBS: Final = {
    "gcc-11": 'echo "gcc-11 (stub) 11.5.0"',
    "g++-11": 'echo "g++-11 (stub) 11.5.0"',
    "rpmbuild": 'echo "RPM version 4.16.1.3"',
    "ctest": "exit 0",
    "cmake": textwrap.dedent(
        """\
        case "$1" in
          --version) echo "cmake version 3.31.0" ;;
          -C) [ -f "$2" ] && grep -q CPACK_PACKAGE_VERSION "$2" ;;
          --build|--install) ;;
          *) exit 3 ;;
        esac
        """
    ),
    # Writes the package CPack would name, or $STUB_RPM_NAME. With CPACK_PRE_BUILD_SCRIPTS, stages
    # $STUB_STAGED under the prefix and runs the scripts with the real cmake ($REAL_CMAKE), as
    # CPack does; the package's bytes are then the staged files that are left.
    "cpack": textwrap.dedent(
        """\
        case "$1" in --version) echo "cpack version 3.31.0"; exit 0 ;; esac
        pre=""
        while [ $# -gt 0 ]; do
          [ "$1" = -B ] && out="$2"
          case "$1" in -D) case "$2" in CPACK_PRE_BUILD_SCRIPTS=*) pre="${2#*=}" ;; esac ;; esac
          shift
        done
        mkdir -p "$out"
        content=rpm-bytes
        if [ -n "$pre" ]; then
          tmp="$out/_CPack_Packages/Linux/RPM/pkg"
          for f in $STUB_STAGED; do mkdir -p "$(dirname "$tmp/opt/ecmwf/$f")"; echo x > "$tmp/opt/ecmwf/$f"; done
          "$REAL_CMAKE" -DCPACK_TEMPORARY_DIRECTORY="$tmp" -P "$pre" || exit 1
          content="$(cd "$tmp" && find . -type f | sort)"
        fi
        printf '%s\\n' "$content" > "$out/${STUB_RPM_NAME:-pkg-2.5.0~rc1-1.x86_64.rpm}"
        """
    ),
}


def _run_stubbed(tmp_path: Path, release_extra: str = "", **env: str) -> subprocess.CompletedProcess[str]:
    from ci_infrastructure.render_job import wrap

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in _STUBS.items():
        stub = bin_dir / name
        stub.write_text("#!/bin/bash\n" + body)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    leg = _release_leg(tmp_path, release_extra=release_extra)
    script = tmp_path / "job.sh"
    script.write_text(wrap(_render(leg), leg=leg, execution="runner"))
    run_env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CI_SOURCE_DIR": str(tmp_path),
        "CI_BUILD_DIR": str(tmp_path / "build"),
        "CI_INSTALL_PREFIX": str(tmp_path / "out"),
        "CI_IMAGE_NAME": "rocky9",
        "CI_IMAGE_TAG": "abc1234",
        **env,
    }
    return subprocess.run(["bash", str(script)], cwd=tmp_path, env=run_env, capture_output=True, text=True)


def test_the_script_packages_exactly_one_rpm_with_its_digest_and_metadata(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "stale.rpm").write_text("from an earlier attempt")
    result = _run_stubbed(tmp_path)
    assert result.returncode == 0, result.stderr
    out = tmp_path / "out"
    assert sorted(p.name for p in out.iterdir()) == ["SHA256SUMS", "build-meta.json", "pkg-2.5.0~rc1-1.x86_64.rpm"]
    assert (out / "SHA256SUMS").read_text().endswith("  pkg-2.5.0~rc1-1.x86_64.rpm\n")
    meta = json.loads((out / "build-meta.json").read_text())
    assert meta["release"]["filename"] == "pkg-2.5.0~rc1-1.x86_64.rpm"
    assert meta["sha256"] == (out / "SHA256SUMS").read_text().split()[0]
    assert meta["image"] == {"name": "rocky9", "tag": "abc1234", "created": ""}
    assert meta["tools"]["cmake"] == "cmake version 3.31.0"


def test_the_script_refuses_a_compiler_cache(tmp_path: Path) -> None:
    result = _run_stubbed(tmp_path, SCCACHE_BUCKET="bucket")
    assert result.returncode != 0
    assert "SCCACHE_BUCKET is set" in result.stderr


def test_the_script_refuses_a_package_named_otherwise(tmp_path: Path) -> None:
    result = _run_stubbed(tmp_path, STUB_RPM_NAME="pkg-2.5.0-1.x86_64.rpm")
    assert result.returncode != 0
    assert "expected pkg-2.5.0~rc1-1.x86_64.rpm" in result.stderr.replace("'", "")


needs_cmake = pytest.mark.skipif(shutil.which("cmake") is None, reason="needs cmake")

_STAGED: Final = "lib64/pkgconfig/pkg.pc lib64/pkgconfig/pkg_test_custom.pc bin/pkg-version"


@needs_cmake
def test_the_script_leaves_excluded_files_out_of_the_package(tmp_path: Path) -> None:
    result = _run_stubbed(tmp_path, release_extra=EXCLUDE, REAL_CMAKE=str(shutil.which("cmake")), STUB_STAGED=_STAGED)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "removing /opt/ecmwf/lib64/pkgconfig/pkg_test_custom.pc" in result.stdout
    packaged = (tmp_path / "out" / "pkg-2.5.0~rc1-1.x86_64.rpm").read_text().split()
    assert packaged == ["./opt/ecmwf/bin/pkg-version", "./opt/ecmwf/lib64/pkgconfig/pkg.pc"]


@needs_cmake
def test_the_script_fails_on_an_exclusion_that_matches_nothing(tmp_path: Path) -> None:
    result = _run_stubbed(
        tmp_path,
        release_extra='exclude = ["lib64/pkgconfig/pkg_test_*.pc", "lib/pkgconfig/pkg_test_*.pc"]\n',
        REAL_CMAKE=str(shutil.which("cmake")),
        STUB_STAGED=_STAGED,
    )
    assert result.returncode != 0
    assert "matched nothing in the install tree" in result.stdout + result.stderr
    assert "lib/pkgconfig/pkg_test_*.pc" in result.stdout + result.stderr
    assert not (tmp_path / "out" / "pkg-2.5.0~rc1-1.x86_64.rpm").exists()


def test_the_script_fails_when_cpack_skips_the_exclusion(tmp_path: Path) -> None:
    """An older CPack ignores CPACK_PRE_BUILD_SCRIPTS: the job must notice, not ship the file."""
    result = _run_stubbed(tmp_path, release_extra=EXCLUDE, REAL_CMAKE="/bin/true", STUB_STAGED=_STAGED)
    assert result.returncode != 0
    assert "CPack did not run the [release] exclude script" in result.stderr


def test_the_script_refuses_to_replace_the_projects_own_pre_build_scripts(tmp_path: Path) -> None:
    """cpack -D replaces a project's CPACK_PRE_BUILD_SCRIPTS: refuse rather than silently drop its hook."""
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "CPackConfig.cmake").write_text(
        'set(CPACK_GENERATOR "RPM")\nset(CPACK_PRE_BUILD_SCRIPTS "/src/project-hook.cmake")\n'
    )
    result = _run_stubbed(tmp_path, release_extra=EXCLUDE, REAL_CMAKE="/bin/true", STUB_STAGED=_STAGED)
    assert result.returncode != 0
    assert "cannot be combined with the project's own CPACK_PRE_BUILD_SCRIPTS" in result.stderr
    assert not (tmp_path / "build" / "_cpack").exists()


# --- _checks.sh.j2 -------------------------------------------------------------------------------------------


def test_checks_hold_no_jinja_and_include_unchanged() -> None:
    source = CHECKS.read_text()
    env = jobscript.job_template_environment(release=True)
    assert env.get_template("ci-infrastructure/_checks.sh.j2").render() == source
    assert not [tok for tok in ("{{", "{%", "{#") if tok in source]


def _checks(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f". {CHECKS}\n{script}"], cwd=tmp_path, capture_output=True, text=True, env={**os.environ}
    )


needs_cc = pytest.mark.skipif(
    shutil.which("cc") is None or shutil.which("readelf") is None or shutil.which("ldd") is None,
    reason="needs cc, readelf and ldd",
)


def _cc(*args: str) -> None:
    subprocess.run(["cc", *args], check=True, capture_output=True)


@needs_cc
def test_runpath_check(tmp_path: Path) -> None:
    prefix = tmp_path / "opt" / "x"
    (prefix / "bin").mkdir(parents=True)
    src = tmp_path / "main.c"
    src.write_text("int main(void) { return 0; }\n")
    _cc(
        str(src),
        "-o",
        str(prefix / "bin" / "good"),
        f"-Wl,-rpath,{prefix}/lib:$ORIGIN/../lib",
        "-Wl,--enable-new-dtags",
    )
    assert _checks(f"ci_release_check_runpath {prefix} {prefix}", tmp_path).returncode == 0
    _cc(str(src), "-o", str(prefix / "bin" / "bad"), "-Wl,-rpath,/tmp/build/lib", "-Wl,--enable-new-dtags")
    result = _checks(f"ci_release_check_runpath {prefix} {prefix}", tmp_path)
    assert result.returncode == 1
    assert "/tmp/build/lib" in result.stderr and "bin/bad" in result.stderr
    (prefix / "bin" / "bad").unlink()
    # A `..` component escapes the allowed prefix, though the text starts with it.
    _cc(
        str(src), "-o", str(prefix / "bin" / "dotdot"), f"-Wl,-rpath,{prefix}/../../usr/lib64", "-Wl,--enable-new-dtags"
    )
    result = _checks(f"ci_release_check_runpath {prefix} {prefix}", tmp_path)
    assert result.returncode == 1 and "/../../usr/lib64" in result.stderr and "bin/dotdot" in result.stderr
    (prefix / "bin" / "dotdot").unlink()
    # An empty entry is the current directory to the loader.
    _cc(str(src), "-o", str(prefix / "bin" / "empty"), f"-Wl,-rpath,:{prefix}/lib", "-Wl,--enable-new-dtags")
    result = _checks(f"ci_release_check_runpath {prefix} {prefix}", tmp_path)
    assert result.returncode == 1 and "empty RUNPATH entry" in result.stderr and "bin/empty" in result.stderr
    (prefix / "bin" / "empty").unlink()
    # A relative entry is relative to the current directory, not to the prefix.
    _cc(str(src), "-o", str(prefix / "bin" / "rel"), "-Wl,-rpath,lib", "-Wl,--enable-new-dtags")
    assert _checks(f"ci_release_check_runpath {prefix} {prefix}", tmp_path).returncode == 1
    (prefix / "bin" / "rel").unlink()
    assert _checks(f"ci_release_check_runpath {prefix} {prefix}", tmp_path).returncode == 0


@needs_cc
def test_ldd_check(tmp_path: Path) -> None:
    lib = tmp_path / "libfoo.so"
    (tmp_path / "foo.c").write_text("int foo(void) { return 1; }\n")
    (tmp_path / "main.c").write_text("int foo(void);\nint main(void) { return foo(); }\n")
    _cc("-shared", "-fPIC", str(tmp_path / "foo.c"), "-o", str(lib))
    prefix = tmp_path / "p"
    prefix.mkdir()
    _cc(str(tmp_path / "main.c"), "-o", str(prefix / "main"), f"-L{tmp_path}", "-lfoo")
    assert _checks(f"ci_release_check_ldd {prefix}", tmp_path).returncode == 1
    _cc(str(tmp_path / "main.c"), "-o", str(prefix / "main"), f"-L{tmp_path}", "-lfoo", f"-Wl,-rpath,{tmp_path}")
    assert _checks(f"ci_release_check_ldd {prefix}", tmp_path).returncode == 0


def test_leak_check(tmp_path: Path) -> None:
    cmake_dir = tmp_path / "p" / "lib" / "cmake" / "pkg"
    cmake_dir.mkdir(parents=True)
    (cmake_dir / "pkg-config.cmake").write_text('set(PKG_INCLUDE "${PACKAGE_PREFIX_DIR}/include")\n')
    assert _checks(f"ci_release_check_leaks {tmp_path / 'p'}", tmp_path).returncode == 0
    (cmake_dir / "pkg-targets.cmake").write_text('set(_IMPORT "/__w/_temp/release-source-deps/ecbuild")\n')
    result = _checks(f"ci_release_check_leaks {tmp_path / 'p'}", tmp_path)
    assert result.returncode == 1 and "pkg-targets.cmake" in result.stderr
    (cmake_dir / "pkg-targets.cmake").unlink()
    (cmake_dir / "pkg.pc").write_text("prefix=/scratch/tmp123/build\n")
    assert _checks(f"ci_release_check_leaks {tmp_path / 'p'} /scratch/tmp123", tmp_path).returncode == 1


def test_paths_check(tmp_path: Path) -> None:
    ok = "printf '%s\\n' /opt /opt/ecmwf /opt/ecmwf/bin/x | ci_release_check_paths /opt/ecmwf"
    assert _checks(ok, tmp_path).returncode == 0
    bad = (
        "printf '%s\\n' /opt/ecmwf/bin/x /usr/bin/x /opt/ecmwfx /usr/lib/.build-id/zz /usr/lib/.build-id/06/evil"
        " | ci_release_check_paths /opt/ecmwf"
    )
    result = _checks(bad, tmp_path)
    assert result.returncode == 1
    assert "/usr/bin/x is outside" in result.stderr and "/opt/ecmwfx is outside" in result.stderr
    assert "/usr/lib/.build-id/zz is outside" in result.stderr
    assert "/usr/lib/.build-id/06/evil is outside" in result.stderr


def test_paths_check_build_id_links(tmp_path: Path) -> None:
    """rpmbuild's /usr/lib/.build-id entries pass only as installed symlinks into the prefix."""
    root = tmp_path / "root"
    (root / "opt/ecmwf/lib").mkdir(parents=True)
    (root / "opt/ecmwf/lib/libx.so").write_text("x")
    (root / "etc").mkdir()
    (root / "etc/shadow").write_text("x")
    ids = root / "usr/lib/.build-id/0f"
    ids.mkdir(parents=True)
    good, regular, outside, dangling = (f"/usr/lib/.build-id/0f/{c * 38}" for c in "abcd")
    (root / good.lstrip("/")).symlink_to("../../../../opt/ecmwf/lib/libx.so")
    (root / regular.lstrip("/")).write_text("not a link")
    (root / outside.lstrip("/")).symlink_to("../../../../etc/shadow")
    (root / dangling.lstrip("/")).symlink_to("../../../../opt/ecmwf/lib/gone.so")

    def run(*paths: str) -> subprocess.CompletedProcess[str]:
        listing = " ".join(paths)
        return _checks(
            f"export CI_RELEASE_TEST_ROOT={root}; printf '%s\\n' {listing} | ci_release_check_paths /opt/ecmwf",
            tmp_path,
        )

    assert run("/usr/lib/.build-id", "/usr/lib/.build-id/0f", good, good + ".1").returncode == 1  # .1 absent
    assert run("/usr/lib/.build-id", "/usr/lib/.build-id/0f", good).returncode == 0
    for path in (regular, outside, f"/usr/lib/.build-id/0f/{'e' * 38}"):
        result = run(path)
        assert result.returncode == 1
        assert f"{path} is not an installed symlink into /opt/ecmwf" in result.stderr
    # A dangling link inside the prefix still resolves (readlink -f) under it: rpm owns the target path.
    assert run(dangling).returncode == 0
    # The shard directories must be real directories, not files or links.
    (root / "usr/lib/.build-id/1a").write_text("payload")
    (root / "usr/lib/.build-id/2b").symlink_to("/etc")
    for path in ("/usr/lib/.build-id/1a", "/usr/lib/.build-id/2b", "/usr/lib/.build-id/3c"):
        result = run(path)
        assert result.returncode == 1
        assert f"{path} is not an installed directory" in result.stderr


def test_json_helper(tmp_path: Path) -> None:
    out = _checks("ci_release_json 'a\"b\\c'", tmp_path).stdout
    assert json.loads(out) == 'a"b\\c'
