.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Enforcing the PR Contributor Declaration
========================================

Public ECMWF repos inherit a PR template from
`ecmwf/.github <https://github.com/ecmwf/.github/blob/main/.github/PULL_REQUEST_TEMPLATE.md>`__
whose last section is the Contributor Declaration — the CLA affirmation plus the
contributor checklist. To fail pull requests whose description does not end with
that block verbatim, drop this file into a repo as
``.github/workflows/contributor-declaration.yml``:

.. code:: yaml

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

No secrets, no tokens, and no configuration: the description is read from the
event payload. A repo whose template has not yet converged on the org block can
point the check at its own copy with
``with: {declaration-file: .github/PULL_REQUEST_TEMPLATE.md}``, and dependabot-style
bot PRs are skipped by default. See
:action:`check-pr-declaration` for the
matching rules, the known gaps, and the full input list.
