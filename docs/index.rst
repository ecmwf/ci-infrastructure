.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

ci-infrastructure
=================

Shared CI orchestration for ECMWF's package graph: a Python package
whose CLIs resolve cross-repo dependencies, move build artifacts through S3,
submit builds to HPC, generate the downstream workflows, and the composite
GitHub Actions that wire it into workflow YAML.

.. note::

   This documentation is provisionally hosted at
   https://sites.ecmwf.int/docs/dev-section/ci-infrastructure/latest,
   and its URL will change.

.. toctree::
   :maxdepth: 2

   introduction

.. toctree::
   :maxdepth: 2

   using/index

.. toctree::
   :maxdepth: 2

   configuring/index

.. toctree::
   :caption: Reference
   :maxdepth: 1

   reference/manifest
   reference/runners
   reference/actions/index
   reference/cli/index
   reference/hpc
   reference/templates
