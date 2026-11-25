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
The organisation baseline below provides most of them;
``scripts/apply-pr-rules.sh`` adds a repository's required checks, labels, and merge settings.

Everyone, on a protected branch
-------------------------------

- Changes only via PR; no direct push
- No force-push, no deletion of the protected branch
- Branch need not be up to date with the protected branch; it must merge without conflicts
- Required checks green: ``ci-result``, ``downstream/runner`` and ``downstream/hpc-atos`` (see :doc:`downstream-ci`; a lane without consumers is green at once),
  and ``ci-result/hpc-atos`` in a repository with HPC legs (the label ``run-hpc-ci`` runs them and ``hpc-ci-not-needed`` waives them, see :doc:`../configuring/ci-yml`)
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

- The organisation baseline below applies on top; a repository cannot relax it
- Who can approve: Settings > Collaborators and teams (Write or higher)
- Code owners: if a ``CODEOWNERS`` file covers a changed file, the approval must come from one of its owners; without one, any approval counts
- ``ci-result`` is a status from the ``collect results`` job in ``ci.yml``; skipped counts as failed
- Fork PRs: ``ci-result`` comes from ``ci-fork.yml`` (see :doc:`fork-pull-requests`), else a maintainer bypasses

Organisation baseline
---------------------

``scripts/apply-org-rules.sh`` applies three `organisation rulesets <https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets>`_
to the same branches of every repository, with or without the new CI.
They protect against malicious or compromised accounts; the repository rules above add the CI checks.

- ``org-branch-integrity``: no deletion, no force-push; no bypass, not even for organisation owners
- ``org-branch-pr``: changes only via PR, with one approval that does not come from the last pusher
  and is dismissed by a new push.
  Organisation owners, admins, gatekeepers, and maintainers are exempt: they merge without it and without a bypass tick.
- ``org-branch-codeowners``: a PR that touches a file the repository's ``CODEOWNERS`` covers
  also needs the approval of a `code owner <https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners>`_,
  dismissed by a new push. Maintainers are not exempt here;
  only organisation owners, admins, and gatekeepers may merge without it (bypass tick).
  Since it also requires a PR, nobody pushes directly, the exempt roles included.

A bypass covers a whole ruleset, hence two: the tick stays the exception for code-owned files,
while normal PRs of maintainers and admins need none.
Each repository's ``CODEOWNERS`` gives ``/.github/``, ``/.ci/``, and ``CODEOWNERS`` to its maintainers,
so a maintainer needs a second one for those; a catch-all ``*`` line makes every PR code-owned.

.. list-table::
   :header-rows: 1

   * -
     - organisation owners
     - admins
     - gatekeepers [#gk]_
     - maintainers
     - write
     - read, forks
     - bot apps [#bots]_
   * - push to / force-push / delete protected branch
     -
     -
     -
     -
     -
     -
     -
   * - open a PR from a branch in the repository
     - X
     - X
     - X
     - X
     - X
     -
     - X
   * - open a PR from a fork
     - X
     - X
     - X
     - X
     - X
     - X
     -
   * - merge a PR without approval, no bypass tick, unless code-owned
     - X
     - X
     - X
     - X
     -
     -
     -
   * - merge after a valid approval
     - X
     - X
     - X
     - X
     - X
     -
     - X
   * - merge a code-owned PR without a valid approval (bypass tick)
     - X
     - X
     - X
     -
     -
     -
     -
   * - give an approval that counts [#approval]_
     - X
     - X
     - X
     - X
     - X
     -
     -
   * - edit or disable the organisation rulesets
     - X
     -
     -
     -
     -
     -
     -
   * - add stricter repository rules
     - X
     - X
     -
     -
     -
     -
     -

.. [#gk] The ``GateKeeper`` custom role, if both scripts run with ``--admin-role`` and its id;
   a custom role inherits neither the admins' bypass nor the maintainers' exemption.
.. [#bots] The workflow regenerator, Dependabot, and ``GITHUB_TOKEN``: no bypass, so their PRs need a human approval.
.. [#approval] Not from the PR's author or its last pusher, with no push after it, and from a code owner where ``CODEOWNERS`` applies.

Not covered: other branches (unrestricted), tags (a separate organisation ruleset), and required checks (repository rules above).

Limitation: write and approval are tied
---------------------------------------

- GitHub ties approving to write access: whoever can push branches and merge can also give a counting approval
- There is no role "write, but cannot approve"; custom roles cannot remove it
- Admins choose per repo who gets write:

  - whole development_section: everyone can push, merge and approve
  - selected people only: the rest open PRs from forks and cannot approve
