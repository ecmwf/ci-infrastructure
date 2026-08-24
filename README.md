<!--
SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)

SPDX-License-Identifier: Apache-2.0
-->

# ci-infrastructure

> [!IMPORTANT]
> This software is **Emerging** and subject to ECMWF's guidelines on
> [Software Maturity](https://github.com/ecmwf/codex/raw/refs/heads/main/Project%20Maturity).

Shared CI orchestration scripts and reusable GitHub Actions for building and
testing ECMWF's downstream package graph. It provides:

- **`ci_infrastructure`** (`src/ci_infrastructure`) — a Python package with
  CLIs for resolving cross-repo dependencies, fetching/publishing build
  artifacts to/from S3, generating downstream CI workflows, and orchestrating
  builds on HPC (SLURM) clusters via [troika](https://github.com/ecmwf/troika).
- **`actions/`** — a set of composite GitHub Actions (dependency resolution,
  artifact fetch/publish, HPC build submission, check-run reporting, etc.)
  used to wire the above into workflow YAML.

See [`HPC.md`](HPC.md) for details on the SLURM/HPC execution path, and
[`IMAGES.md`](IMAGES.md) for the container images the CI jobs run inside —
they are built from `public-images/` in this repo, so an image and the
`ci_infrastructure` it carries can never drift apart.

## Scope

This repository only contains CI orchestration plumbing (dependency graph
resolution, artifact caching, workflow generation, HPC job submission) and the
Dockerfiles for the public container images those jobs run inside. It is
**not** a scientific or operational package and does not process or produce
forecast data itself.

Images whose *content* must stay internal — anything with credentials baked in —
live in [`ecmwf/ci-container-images`](https://github.com/ecmwf/ci-container-images)
instead.

## Software maturity & support

This project follows the [ECMWF Software Maturity
classification](https://github.com/ecmwf/codex/blob/main/Software%20Maturity/index.md)
(see the maturity badge at the top of this README).

- **Level of support:** 🔴 **Best effort / none** — maintained by the CI
  infrastructure team as time allows. There is no guaranteed response time or
  SLA, and it is **not officially supported for operational use**.

> [!NOTE]
> This is internal CI tooling, not a supported product. Do not rely on it in
> an operational context. Use at your own risk, and expect breaking changes.

<!-- For questions, issues, or support requests, please contact ECMWF via the
[ECMWF Service Desk](https://support.ecmwf.int/). -->

## Installation

Requires Python >= 3.11.

```bash
pip install .
# or, for running the test suite:
pip install ".[test]"
```

This installs the following console scripts:

```
ci-infrastructure-generate
ci-infrastructure-resolve
ci-infrastructure-fetch
ci-infrastructure-check
ci-infrastructure-print-dep-table
ci-infrastructure-s3
ci-infrastructure-hpc
ci-infrastructure-check-declaration
```

## Example Usage

Resolve the dependency graph for a package and print it as a table:

```bash
ci-infrastructure-resolve --config deps.yml --output resolved.json
ci-infrastructure-print-dep-table resolved.json
```

In a workflow, the same functionality is typically consumed through the
composite actions in [`actions/`](actions), e.g.:

```yaml
- uses: ecmwf/ci-infrastructure/actions/resolve-deps@main
  with:
    config: deps.yml
```

## Enforcing the PR Contributor Declaration

Public ECMWF repos inherit a PR template from
[`ecmwf/.github`](https://github.com/ecmwf/.github/blob/main/.github/PULL_REQUEST_TEMPLATE.md)
whose last section is the Contributor Declaration — the CLA affirmation plus the
contributor checklist. To fail pull requests whose description does not end with
that block verbatim, drop this file into a repo as
`.github/workflows/contributor-declaration.yml`:

```yaml
name: Contributor Declaration

on:
  # pull_request_target runs the BASE branch's copy of this file, so a pull
  # request cannot edit the gate that judges it. Safe here because nothing from
  # the pull request is ever checked out or executed and no write token is used.
  pull_request_target:
    # `edited` is what makes the gate real: a description can be emptied with no
    # push at all. `synchronize` is needed for a different reason — required
    # checks are per-SHA, so a push without a run leaves the check pending.
    types: [opened, edited, reopened, synchronize]

permissions:
  contents: read

jobs:
  contributor-declaration:
    uses: ecmwf/ci-infrastructure/.github/workflows/check-pr-declaration.yml@main
```

No secrets, no tokens, and no configuration: the description is read from the
event payload. A repo whose template has not yet converged on the org block can
point the check at its own copy with
`with: {declaration-file: .github/PULL_REQUEST_TEMPLATE.md}`, and dependabot-style
bot PRs are skipped by default. See
[`actions/check-pr-declaration`](actions/check-pr-declaration/action.yml) for the
matching rules, the known gaps, and the full input list.

## License

[Apache License 2.0](LICENSE) In applying this licence, ECMWF does not
waive the privileges and immunities granted to it by virtue of its status
as an intergovernmental organisation nor does it submit to any
jurisdiction.
