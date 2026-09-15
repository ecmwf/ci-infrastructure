# Workflow experiments (disabled)

GitHub Actions only runs workflow files directly in `.github/workflows/`, so the
files here are parked and never execute.

They are the central CI-approval gate experiment:

- `ci-gated.yml`: the only file with pull request triggers. It holds one
  `test-ci-approval` gate (label `test-approved-for-ci`) and calls both lanes.
- `placeholder-ci.yml`: unit-test lane, `on: workflow_call` only.
- `try-integration-test-gating.yml`: integration-test lane, `on: workflow_call`
  only, opt-in via `run-expensive-tests`.

To re-enable, move all three back into `.github/workflows/` together:
`ci-gated.yml` calls the lanes as `./.github/workflows/<file>`.

While parked they are not checked by the `actionlint` and `check-ci-approval`
pre-commit hooks, which only look at `.github/workflows/`.
