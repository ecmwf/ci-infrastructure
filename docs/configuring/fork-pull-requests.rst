.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Fork pull requests
==================

By default, CI does not run for pull requests from forks.
A fork's code is untrusted, and there are two ways in which it could do harm:

- it runs on ECMWF hardware, i.e. on the self-hosted runners and, through them, on the HPC;
- it reads the secrets of the workflow, e.g. the GitHub App key or the registry credentials.

Under ``pull_request``, a fork's run gets neither secrets nor a token that can write,
so ``ci.yml`` cannot build anything for it,
and :action:`require-ci-approval` keeps it off the self-hosted runners in any case.

A public repository can opt in.
Then a maintainer's ``approved-for-ci`` label says *this commit is trusted*,
and the approved commit runs the repository's own ``ci.yml``, with its secrets and runners,
just like a pull request from a branch of the repository.

.. warning::

   Approve only a commit you have read.
   The approved code runs before it is merged, with the same secrets and runners as the repository's own CI.

Opting in
---------

In the :doc:`manifest`:

.. code:: toml

   [package]
   visibility = "public"
   fork-pull-requests = true

:doc:`ci-infrastructure-generate <../reference/cli/ci-infrastructure-generate>` then writes ``ci-fork.yml`` (and removes it again without the opt-in).
It runs on `pull_request_target <https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request_target>`__,
whose workflow files are always those of the base branch, never the fork's,
and calls ``ci.yml`` as a `reusable workflow <https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows>`__.

``ci.yml`` needs three small changes, shown in :doc:`ci-yml`;
the ``check-ci-approval`` pre-commit hook fails until they are in.

How a fork pull request runs
----------------------------

1. A maintainer reads the change and sets ``approved-for-ci``.
   The label starts ``CI (fork)``; there is nothing to re-run.
2. :action:`require-ci-approval` in ``ci.yml`` spends the label, so it approves exactly one commit.
   A push to the pull request removes a leftover label and runs nothing.
3. The jobs check out the approved head commit with :action:`checkout-under-test`,
   taken from the event, so a push in the meantime does not reach jobs still waiting to start;
   they publish their artifacts under that commit.
   A fork's branch name is never used: :action:`resolve-deps` builds a fork's pull request as its base branch,
   pinned to its head commit, so no ``sync-branch/`` or ``feature/`` branch is matched.
   A change across several repositories has to come from branches in those repositories.

Downstream CI
-------------

The ``run-downstream-ci:*`` labels work as for any pull request, see :doc:`downstream`,
with two differences:

- Setting the label alone starts nothing, since a fork's label event has no secrets.
  Downstream CI starts when an approved ``CI (fork)`` completes,
  so set the label before ``approved-for-ci``, or start the downstream workflow by hand (below).
- It only ever runs for a commit that has an approved ``CI (fork)`` run:
  :action:`resolve-dispatch-context` looks for one whose ``ci-approval`` job passed for exactly that commit.
  A newer, unapproved push therefore has no downstream CI,
  and its artifacts are never rebuilt for it: a pinned commit's missing artifact is an error, not a rebuild.

Upstream repositories never build a fork's code:
their downstream CI builds each consumer at its own branches, which a fork cannot add to.

Starting downstream CI by hand
------------------------------

Every ``trigger-downstream*.yml`` can be started with **Run workflow**, in any repository, with

- ``pull-request``: the number of the pull request,
- ``level``: ``all`` or a level from 1, as in the ``run-downstream-ci:*`` labels.

Only users with write access can start it, and it runs the workflow from a branch of this repository.
The same rule holds: the pull request's head commit needs a successful ``CI`` run,
or for a fork an approved ``CI (fork)`` run.
