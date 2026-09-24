.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Actions never make you bootstrap
================================

A ``uses:`` line is all an action needs — there is no setup step to remember, and
that is a guarantee rather than a coincidence:

   **No action requires its caller to bootstrap.** An action that uses the
   ``ci_infrastructure`` package runs ``actions/ensure-infrastructure-present``
   itself, as its first step. An action that does not use the package does not,
   so it costs you no venv.

`tests/test_action_conventions.py <https://github.com/ecmwf/ci-infrastructure/blob/main/tests/test_action_conventions.py>`__ enforces both halves, so it stays true as
actions are added.

Call ``ensure-infrastructure-present`` yourself only when a workflow runs
``$CI_INFRASTRUCTURE_PYTHON`` **directly** rather than through an action — as this
repo's own ``hpc-nightly-cleanup.yml`` and ``smoke-test-hpc.yml`` do.

Set ``CI_INFRASTRUCTURE_FORCE_REINSTALL=true`` in a job's ``env:`` to install from the
checkout instead of a baked interpreter.
A stale baked package already warns and reinstalls on its own.
