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
from release_fixtures import RECIPE, write_release_repo

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


def _release_leg(tmp_path: Path, tag: str = "2.5.0-rc1") -> dict[str, Any]:
    rm = legs.load(write_release_repo(tmp_path))
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


def test_rendered_release_script_is_valid_bash(tmp_path: Path) -> None:
    script = tmp_path / "job.sh"
    script.write_text(_render(_release_leg(tmp_path)))
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_a_recipe_that_bypasses_the_overlay_is_refused(tmp_path: Path) -> None:
    leg = _release_leg(tmp_path)
    with pytest.raises(jobscript.JobTemplateError, match="without the release overlay"):
        _render(leg, '{% extends "ci-infrastructure/cmake-runner.sh.j2" %}\n')


def test_the_hpc_lane_has_no_release_overlay_yet(tmp_path: Path) -> None:
    with pytest.raises(jobscript.JobTemplateError, match="renders nothing"):
        _render(_release_leg(tmp_path), execution="hpc-atos")


def test_a_leg_field_named_release_is_refused() -> None:
    with pytest.raises(jobscript.JobTemplateError, match="'release'"):
        jobscript.build_template_context({**_CI_LEG, "release": "x"}, execution="runner")


def test_release_must_be_a_table() -> None:
    with pytest.raises(jobscript.JobTemplateError, match="must be a table"):
        jobscript.render_job_template(
            template_source=_CI_RECIPE, template_name="b.sh.j2", leg={**_CI_LEG, "_release": "x"}, execution="runner"
        )


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
    # Writes the package CPack would name, or $STUB_RPM_NAME.
    "cpack": textwrap.dedent(
        """\
        case "$1" in --version) echo "cpack version 3.31.0"; exit 0 ;; esac
        while [ $# -gt 0 ]; do [ "$1" = -B ] && out="$2"; shift; done
        mkdir -p "$out"
        echo rpm-bytes > "$out/${STUB_RPM_NAME:-pkg-2.5.0~rc1-1.x86_64.rpm}"
        """
    ),
}


def _run_stubbed(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    from ci_infrastructure.render_job import wrap

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in _STUBS.items():
        stub = bin_dir / name
        stub.write_text("#!/bin/bash\n" + body)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    leg = _release_leg(tmp_path)
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
    bad = "printf '%s\\n' /opt/ecmwf/bin/x /usr/bin/x /opt/ecmwfx | ci_release_check_paths /opt/ecmwf"
    result = _checks(bad, tmp_path)
    assert result.returncode == 1
    assert "/usr/bin/x is outside" in result.stderr and "/opt/ecmwfx is outside" in result.stderr


def test_json_helper(tmp_path: Path) -> None:
    out = _checks("ci_release_json 'a\"b\\c'", tmp_path).stdout
    assert json.loads(out) == 'a"b\\c'
