# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Rendering a `.j2` HPC recipe against its matrix leg; an undeclared name fails rather than rendering empty."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ci_infrastructure._errors import CIError
from ci_infrastructure.hpc import jobscript
from ci_infrastructure.hpc.orchestrate import resolve_recipe

LEG: dict[str, Any] = {
    "cxx-compiler": "g++-8",
    "build-type": "Release",
    "platform": "hpc-atos-gnu",
    "modules": ["load prgenv/gnu", "unload gcc", "load gcc/old"],
    "cc": "gcc",
    "cxx": "g++",
    "_resolved": {"own-artifact-name": "eckit-deadbeef", "cmake-prefix-path": "/runner/local/dep"},
}


def _render(
    source: str,
    leg: dict[str, Any] | None = None,
    *,
    artifact_name: str = "",
    search_path: Path | None = None,
) -> str:
    return jobscript.render_job_template(
        template_source=source,
        template_name="build.sh.j2",
        leg=LEG if leg is None else leg,
        artifact_name=artifact_name,
        search_path=search_path,
    )


def test_a_plain_sh_recipe_is_passed_through_byte_for_byte(tmp_path: Path) -> None:
    """Even with jinja delimiters in it: `${x//{{/y}}` is legal bash."""
    body = "#!/bin/bash\nawk '{ print $1 }' f\nx=${v//{{/y}}\n"
    script = tmp_path / "build-gnu.sh"
    script.write_text(body)
    assert resolve_recipe(script, matrix_leg='{"cc": "gcc"}', artifact_name="a") == body


def test_leg_values_render_under_normalised_names() -> None:
    out = _render("cc={{ cc }} cxx={{ cxx_compiler }} type={{ build_type }}\n")
    assert out == "cc=gcc cxx=g++-8 type=Release\n"


def test_raw_leg_mapping_reaches_hyphenated_keys() -> None:
    assert _render("{{ leg['cxx-compiler'] }}\n") == "g++-8\n"


def test_artifact_name_is_available() -> None:
    assert _render("{{ artifact_name }}\n", artifact_name="eckit-deadbeef") == "eckit-deadbeef\n"


def test_undeclared_name_fails_and_names_what_the_leg_has() -> None:
    with pytest.raises(jobscript.JobTemplateError) as exc:
        _render("module load {{ fortran_compiler }}\n")
    assert "fortran_compiler" in str(exc.value)
    assert "cxx-compiler" in str(exc.value)


def test_undeclared_subscript_on_the_raw_leg_also_fails() -> None:
    with pytest.raises(jobscript.JobTemplateError):
        _render("{{ leg['nope'] }}\n")


def test_resolved_is_not_in_the_context() -> None:
    """`_resolved` holds runner-local paths no compute node can see."""
    with pytest.raises(jobscript.JobTemplateError):
        _render("{{ _resolved }}\n")


def test_cluster_env_vars_stay_env_vars() -> None:
    assert _render('p="$CI_INSTALL_PREFIX"\n') == 'p="$CI_INSTALL_PREFIX"\n'
    with pytest.raises(jobscript.JobTemplateError):
        _render("{{ CI_INSTALL_PREFIX }}\n")


def test_shell_metacharacters_survive_verbatim() -> None:
    assert _render("a && b > c || d 'e'\n") == "a && b > c || d 'e'\n"


def test_sh_filter_quotes_a_hostile_value() -> None:
    leg = {"ctest-args": "-E 's_test|s_zombies'"}
    assert _render("ctest {{ ctest_args | sh }}\n", leg) == "ctest '-E '\"'\"'s_test|s_zombies'\"'\"''\n"


def test_a_module_loop_leaves_no_blank_lines() -> None:
    """Keeps a templated #SBATCH block contiguous for _split_header."""
    out = _render("    {% for m in modules %}\n    module {{ m }}\n    {% endfor %}\n")
    assert out == "    module load prgenv/gnu\n    module unload gcc\n    module load gcc/old\n"


def test_trailing_newline_is_kept() -> None:
    assert _render("last\n").endswith("last\n")


def test_a_name_colliding_with_a_context_extra_is_refused() -> None:
    with pytest.raises(jobscript.JobTemplateError, match="already defines"):
        _render("x\n", {"leg": "boom"})


def test_two_keys_normalising_to_one_name_are_refused() -> None:
    with pytest.raises(jobscript.JobTemplateError, match="shadow"):
        _render("x\n", {"cxx-compiler": "g++", "cxx_compiler": "clang++"})


def test_a_syntax_error_names_its_line() -> None:
    with pytest.raises(jobscript.JobTemplateError, match="build.sh.j2:2"):
        _render("ok\n{% for x in %}\n")


def test_include_resolves_from_the_recipe_directory(tmp_path: Path) -> None:
    (tmp_path / "_epilogue.sh.j2").write_text("tar -C {{ cc }} .\n")
    out = _render('{% include "_epilogue.sh.j2" %}\n', search_path=tmp_path)
    assert out == "tar -C gcc .\n"


def test_undeclared_names_are_found_statically_in_a_dead_branch() -> None:
    src = "{% if false %}{{ never_declared }}{% endif %}{{ cc }}\n"
    assert jobscript.undeclared_template_names(src, LEG, template_name="t") == {"never_declared"}
    assert jobscript.undeclared_template_names("{{ cc }}{{ leg }}\n", LEG, template_name="t") == set()


def test_rendered_template_keeps_its_shebang_and_sbatch_header() -> None:
    """A leading {% set %} leaves a blank first line."""
    src = "{% set t = build_type %}\n#!/bin/bash\n\n#SBATCH --qos=nf\n\nmake {{ t }}\n"
    rendered = _render(src)
    wrapped = jobscript.render_job_script(
        repo_script=rendered, output_path="/o", cmake_prefix_path="/p", install_path="/i"
    )
    lines = wrapped.splitlines()
    assert lines[0] == "#!/bin/bash"
    assert lines.count("#!/bin/bash") == 1
    assert "#SBATCH --qos=nf" in lines
    assert lines.index("#SBATCH --output=/o") < lines.index("set -euo pipefail")
    assert "make Release" in wrapped


def test_template_without_a_leg_is_a_named_error(tmp_path: Path) -> None:
    script = tmp_path / "build.sh.j2"
    script.write_text("module load {{ cc }}\n")
    with pytest.raises(CIError, match="--matrix-leg"):
        resolve_recipe(script, matrix_leg="", artifact_name="a")


def test_a_leg_that_is_not_a_json_object_is_a_named_error(tmp_path: Path) -> None:
    script = tmp_path / "build.sh.j2"
    script.write_text("{{ cc }}\n")
    with pytest.raises(CIError, match="must be a JSON object"):
        resolve_recipe(script, matrix_leg='["gcc"]', artifact_name="a")
    with pytest.raises(CIError, match="not valid JSON"):
        resolve_recipe(script, matrix_leg="{oops", artifact_name="a")


def test_a_plain_recipe_ignores_a_leg(tmp_path: Path) -> None:
    script = tmp_path / "build-gnu.sh"
    script.write_text("module load gcc/old\n")
    assert resolve_recipe(script, matrix_leg='{"cc": "icx"}', artifact_name="a") == "module load gcc/old\n"
