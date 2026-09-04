# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Rendering a `.j2` HPC recipe against the matrix leg that selected it.

The point of the feature is that the manifest and the recipe cannot disagree, so
most of these tests are about what happens when they *would*: an undeclared name
must fail loudly rather than render empty. The first test is the other half of the
contract — a plain `.sh` must still come through byte-for-byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ci_infrastructure._errors import CIError
from ci_infrastructure.hpc import jobscript
from ci_infrastructure.hpc.orchestrate import resolve_recipe

LEG = {
    "cxx-compiler": "g++-8",
    "build-type": "Release",
    "platform": "hpc-atos-gnu",
    "modules": ["load prgenv/gnu", "unload gcc", "load gcc/old"],
    "cc": "gcc",
    "cxx": "g++",
    "_resolved": {"own-artifact-name": "eckit-deadbeef", "cmake-prefix-path": "/runner/local/dep"},
}


def _render(source: str, leg: dict | None = None, **kw) -> str:
    return jobscript.render_job_template(
        template_source=source, template_name="build.sh.j2", leg=LEG if leg is None else leg, **kw
    )


def test_a_plain_sh_recipe_is_passed_through_byte_for_byte(tmp_path: Path) -> None:
    """The backwards-compatibility contract: `.j2` is the whole of the opt-in.

    A recipe containing jinja delimiters is the sharp case — `${x//{{/y}}` is legal
    bash — and it must reach the wrapper untouched.
    """
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
    """StrictUndefined is the enforcement. Rendering empty is the bug being fixed:
    an empty `module load` line builds a different binary under an unchanged name."""
    with pytest.raises(jobscript.JobTemplateError) as exc:
        _render("module load {{ fortran_compiler }}\n")
    assert "fortran_compiler" in str(exc.value)
    assert "cxx-compiler" in str(exc.value)  # lists what IS declared


def test_undeclared_subscript_on_the_raw_leg_also_fails() -> None:
    with pytest.raises(jobscript.JobTemplateError):
        _render("{{ leg['nope'] }}\n")


def test_resolved_is_not_in_the_context() -> None:
    """`_resolved` carries RUNNER-local paths. cmake-prefix-path in particular is
    rewritten to the shipped cluster copies after the leg is read, so a template that
    baked it in would point the job at directories no compute node can see."""
    with pytest.raises(jobscript.JobTemplateError):
        _render("{{ _resolved }}\n")


def test_cluster_env_vars_stay_env_vars() -> None:
    """They are not knowable at render time (the work dir is expanded on the
    cluster), so they are not template names — and a recipe using $VAR is untouched."""
    assert _render('p="$CI_INSTALL_PREFIX"\n') == 'p="$CI_INSTALL_PREFIX"\n'
    with pytest.raises(jobscript.JobTemplateError):
        _render("{{ CI_INSTALL_PREFIX }}\n")


def test_shell_metacharacters_survive_verbatim() -> None:
    """autoescape is off: this is shell, not markup."""
    assert _render("a && b > c || d 'e'\n") == "a && b > c || d 'e'\n"


def test_sh_filter_quotes_a_hostile_value() -> None:
    """The corpus really contains one: ecflow's ctest-args carries apostrophes."""
    leg = {"ctest-args": "-E 's_test|s_zombies'"}
    assert _render("ctest {{ ctest_args | sh }}\n", leg) == "ctest '-E '\"'\"'s_test|s_zombies'\"'\"''\n"


def test_a_module_loop_leaves_no_blank_lines() -> None:
    """trim_blocks/lstrip_blocks make a {% %}-only line vanish, which is what keeps a
    templated #SBATCH block the contiguous run of #-lines _split_header needs."""
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
    """Static, so a name inside a never-taken branch still has to be declared — the
    right side to err on when the point is that the manifest says what a leg is."""
    src = "{% if false %}{{ never_declared }}{% endif %}{{ cc }}\n"
    assert jobscript.undeclared_template_names(src, LEG, template_name="t") == {"never_declared"}
    assert jobscript.undeclared_template_names("{{ cc }}{{ leg }}\n", LEG, template_name="t") == set()


def test_rendered_template_keeps_its_shebang_and_sbatch_header() -> None:
    """A leading {% set %} can leave a blank first line; the shebang must still be
    recognised rather than demoted into the comment block (which would leave the real
    shebang inert under an injected one)."""
    src = "{% set t = build_type %}\n#!/bin/bash\n\n#SBATCH --qos=nf\n\nmake {{ t }}\n"
    rendered = _render(src)
    wrapped = jobscript.render_job_script(
        repo_script=rendered, output_path="/o", cmake_prefix_path="/p", install_path="/i"
    )
    lines = wrapped.splitlines()
    assert lines[0] == "#!/bin/bash"
    assert lines.count("#!/bin/bash") == 1
    assert "#SBATCH --qos=nf" in lines
    # our injected directives must still land inside the header, before any command
    assert lines.index("#SBATCH --output=/o") < lines.index("set -euo pipefail")
    assert "make Release" in wrapped


def test_split_header_skips_leading_blank_lines() -> None:
    shebang, header, body = jobscript._split_header("\n\n#!/bin/bash\n#SBATCH --qos=nf\nmake\n")
    assert shebang == "#!/bin/bash"
    assert "#SBATCH --qos=nf" in header
    assert body == ["make"]


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
