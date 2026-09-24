# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent
sys.path.insert(0, str(DOCS / "_ext"))

project = "ci-infrastructure"
copyright = "2026, European Centre for Medium-Range Weather Forecasts (ECMWF)"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx_click",
    "ghactions",
    "clis",
    "manifest_schema",
]

exclude_patterns = ["_build"]
html_theme = "furo"

# Docstrings and action descriptions write `code` in Markdown style.
default_role = "literal"
nitpicky = True
nitpick_ignore_regex = [
    # No inventories.
    ("py:class", r"(yaml|troika|boto3|botocore|mypy_boto3_s3)\..*"),
    # NewType aliases and private helpers named in public signatures.
    ("py:class", r"ci_infrastructure\.(\w+\.)*_\w+"),
    ("py:class", r"(Repo|Ref|Sha|PackageName|ArtifactName|Step|NodeT|Execution|Visibility)"),
    ("py:class", r"ci_infrastructure\.\w+\.(Repo|Ref|Sha|PackageName|ArtifactName|Step|NodeT|Execution|Visibility)"),
]

autodoc_mock_imports = ["troika", "boto3", "botocore"]
autodoc_typehints = "description"
autodoc_typehints_description_target = "all"
autodoc_member_order = "bysource"

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "click": ("https://click.palletsprojects.com/en/stable", None),
    "jinja2": ("https://jinja.palletsprojects.com/en/stable", None),
    "pydantic": ("https://docs.pydantic.dev/latest", None),
}

ghactions_repo = "ecmwf/ci-infrastructure"
ghactions_ref = "main"
ghactions_root = str(DOCS.parent)
ghactions_workflows = [".github/workflows/check-pr-declaration.yml"]
# (heading, one-line intro, actions): the sections of the composite-action index.
ghactions_groups = [
    (
        "Checkout and build",
        "Get the right sources and build them.",
        ["checkout-under-test", "pick-ref", "pre-commit", "cmake-build", "setup-sccache"],
    ),
    (
        "Dependencies",
        "Resolve the dependency tree. Move install trees through S3.",
        ["resolve-deps", "fetch-deps", "publish-artifact", "install-prefix"],
    ),
    (
        "HPC",
        "Build on the cluster. Move trees to and from it. See :doc:`../../../howto/hpc`.",
        ["build-on-hpc", "push-hpc-tree", "fetch-hpc-tree", "remove-hpc-tree"],
    ),
    (
        "Downstream CI",
        "Used by the generated workflows to fan out to other repos.",
        ["resolve-dispatch-context", "dispatch-and-wait", "report-check-run", "validate-generated-workflows"],
    ),
    (
        "PR gates",
        "Decide whether a pull request may run CI or downstream CI.",
        ["require-ci-approval", "require-label-decision", "check-pr-label", "check-pr-declaration"],
    ),
    ("Logging", "Say what a job runs in and what it built against.", ["announce-image", "print-dep-table"]),
    (
        "Internals",
        "Nested by other actions. See :doc:`../../../explanation/bootstrap`.",
        ["ensure-infrastructure-present"],
    ),
]


def _drop_model_signature(app, what, name, obj, options, signature, return_annotation):
    """Pydantic models: the keyword-only __init__ repeats the fields, with postponed annotations."""
    from pydantic import BaseModel

    if what == "class" and isinstance(obj, type) and issubclass(obj, BaseModel):
        return "", None
    return None


def _skip_member(app, what, name, obj, skip, options):
    # Hyphenated keys of functional TypedDicts are not attributes.
    return True if name == "model_config" or not name.isidentifier() else None


def setup(app):
    app.connect("autodoc-process-signature", _drop_model_signature)
    app.connect("autodoc-skip-member", _skip_member)
