.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

PR merge rules for the protected branches
==========================================

.. note::

   These rules are pending discussion.

These rules are the default setting; the admins (gatekeepers) of a repository can adjust them.
They protect only ``main``, ``develop``, ``master`` and the default branch, if it is named otherwise,
and PRs into them. Other branches are unrestricted.
``scripts/apply-pr-rules.sh`` applies them to a repository.

Everyone, on a protected branch
-------------------------------

- Changes only via PR; no direct push
- No force-push, no deletion of the protected branch
- Branch must be up to date with the protected branch (merge or rebase)
- Required checks green: ``ci-result``, ``downstream/runner`` and ``downstream/hpc-atos`` (see :doc:`downstream-ci`; a lane without consumers is green at once)
- Merged branches are deleted automatically
- Auto-merge available

By role, on a protected branch
------------------------------

.. list-table::
   :header-rows: 1

   * -
     - admins (gatekeepers)
     - maintainers
     - write (developers)
     - read
   * - push branches, merge after approval + green CI
     - X
     - X
     - X
     -
   * - own PR needs an approval
     -
     -
     - X
     -
   * - approval counts
     - X
     - X
     - X
     -
   * - merge red CI / out-of-date branch (bypass tick)
     - X
     - X
     -
     -
   * - push to / force-push / delete protected branch
     -
     -
     -
     -
   * - edit rules
     - X
     -
     -
     -

- Read: PRs from forks only

Notes
-----

- Who can approve: Settings > Collaborators and teams (Write or higher)
- ``ci-result`` is a status from the ``collect results`` job in ``ci.yml``; skipped counts as failed
- Fork PRs: ``ci-result`` comes from ``ci-fork.yml`` (see :doc:`fork-pull-requests`), else a maintainer bypasses

Limitation: write and approval are tied
---------------------------------------

- GitHub ties approving to write access: whoever can push branches and merge can also give a counting approval
- There is no role "write, but cannot approve"; custom roles cannot remove it
- Admins choose per repo who gets write:

  - whole development_section: everyone can push, merge and approve
  - selected people only: the rest open PRs from forks and cannot approve
