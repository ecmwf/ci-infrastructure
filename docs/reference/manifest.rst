.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Manifest
========

Every repo in the graph describes itself in `.ci/manifest.toml`. Both
:doc:`ci-infrastructure-generate <cli/ci-infrastructure-generate>` and
:doc:`ci-infrastructure-resolve <cli/ci-infrastructure-resolve>` validate it
against the tables below and reject unknown keys; this page is generated from
the pydantic schema in
`src/ci_infrastructure/manifest.py <https://github.com/ecmwf/ci-infrastructure/blob/main/src/ci_infrastructure/manifest.py>`__.

.. manifest-schema::

Rules across manifests
----------------------

:doc:`ci-infrastructure-generate <cli/ci-infrastructure-generate>` also checks the graph as a whole:

- `[package].name` and `[package].repo` are unique across the manifests in scope.
- A `needs` entry `<kind>` names a kind of the same manifest. An entry
  `<package-name>/<kind>` names a kind with `triggers` in that package's
  manifest, which must list this repo in its `[[trigger-downstream]]`;
  cross-repo `needs` have no cycles.
- Every repo named in `[[trigger-downstream]]` lists this one in its `[[deps]]`,
  and `[[trigger-downstream]]` has no cycles.
- A kind with `triggers` or `reuse-matrix` has at least one leg, and no two legs
  of a publishing kind produce the same artifact name.
