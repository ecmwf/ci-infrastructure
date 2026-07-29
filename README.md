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

See [`HPC.md`](HPC.md) for details on the SLURM/HPC execution path.

## Scope

This repository only contains CI orchestration plumbing (dependency graph
resolution, artifact caching, workflow generation, HPC job submission). It is
**not** a scientific or operational package and does not process or produce
forecast data itself.

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

Requires Python >= 3.9.

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
