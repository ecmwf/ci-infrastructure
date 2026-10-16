.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Fork PRs
========

A pull request from a fork runs no CI by itself:
its code would run with the repository's secrets and on ECMWF hardware.
In a repository that takes fork pull requests (see :doc:`../configuring/fork-pull-requests`),
a maintainer approves a commit, and then it runs the repository's own ``CI``.

Approving a commit
------------------

Every push to the pull request starts ``CI (fork)``, which fails at its ``ci-approval`` job.

1. Read the commit that run is for: its page names it.
2. Re-run it, *Re-run all jobs*. Re-running needs write access to the repository.

The re-run approves exactly that commit. A new push starts a new run for the new commit:
review the new commits (GitHub's *Changes since your last review* shows them), then re-run that run.

Downstream CI
-------------

The ``run-downstream-ci:*`` labels work as described in :doc:`downstream-ci`,
but only for a commit with an approved ``CI (fork)`` run:
a label set before the approval takes effect once the approved run has completed.
The repository's ``Downstream`` workflows can also be started by hand with **Run workflow**,
giving the pull request's number and the level.

A fork's branch name is never matched: its pull request is built against the branches the manifests name,
usually ``develop``. A change that needs branches in several repositories has to come from branches of those
repositories, see :doc:`feature-branches`.
