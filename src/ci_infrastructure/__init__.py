"""ci-infrastructure: shared CI orchestration scripts and Composite Actions.

The CLI entry points are exposed as `ci-infrastructure-generate`, `ci-infrastructure-resolve`,
`ci-infrastructure-fetch`, `ci-infrastructure-check`, and `ci-infrastructure-print-dep-table` —
see `pyproject.toml`'s `[project.scripts]`. The composite actions in `actions/`
invoke these via `$CI_INFRASTRUCTURE_PYTHON -m ci_infrastructure.<module>`, where
`$CI_INFRASTRUCTURE_PYTHON` is the venv interpreter provisioned by the
`ensure-infrastructure-present` composite.
"""
