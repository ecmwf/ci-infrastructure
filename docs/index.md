<!--
SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)

SPDX-License-Identifier: Apache-2.0
-->

# ci-infrastructure

Shared CI orchestration for ECMWF's downstream package graph: a Python package
whose CLIs resolve cross-repo dependencies, move build artifacts through S3,
generate the downstream workflows and submit builds to HPC, and the composite
GitHub Actions that wire it into workflow YAML.

```{toctree}
:caption: Tutorial
:maxdepth: 2

tutorial/index
```

```{toctree}
:caption: How-to guides
:maxdepth: 1

howto/hpc
howto/images
howto/fork-prs
howto/contributor-declaration
```

```{toctree}
:caption: Explanation
:maxdepth: 1

explanation/bootstrap
```

```{toctree}
:caption: Reference
:maxdepth: 1

reference/manifest
reference/runners
reference/actions
reference/cli
reference/python
```
