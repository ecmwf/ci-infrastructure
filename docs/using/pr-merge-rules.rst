.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

PR merge rules for the default branch
=====================================

.. note::

   These rules are pending discussion.

These rules are the default setting; the maintainers of a repository can adjust them.
They protect only the default branch (``main``/``master``/``develop``) and PRs into it.
Other branches are unrestricted.
``scripts/apply-pr-rules.sh`` applies them to a repository.

Live on: ci-infrastructure, stack-dependencies

Everyone, on the default branch
-------------------------------

- Changes only via PR; no direct push
- No force-push, no deletion of the default branch
- Branch must be up to date with the default branch (merge or rebase)
- Required checks green: ``ci-result`` (+ ``downstream-ci-label`` where :doc:`downstream-ci` exists)
- Merged branches are deleted automatically
- Auto-merge available

By role, on the default branch
------------------------------

.. list-table::
   :header-rows: 1

   * -
     - admins
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
   * - push to / force-push / delete default branch
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
