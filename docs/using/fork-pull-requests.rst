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

1. Read the change, and submit a review of it on GitHub with *Comment*;
   there is no need to approve the pull request before its CI has run.
2. Set the label ``approved-for-ci``. It starts ``CI (fork)``; there is nothing to re-run.

Both need write access to the repository.
The label approves exactly the head commit you reviewed, and only once:
it is removed when the run starts.

After a new push
----------------

A push to the pull request removes a leftover label and runs nothing.
Review the new commits (GitHub's *Changes since your last review* shows them), then set ``approved-for-ci`` again.
If the head commit has no review of yours, the run fails and says so.

Downstream CI
-------------

The ``run-downstream-ci:*`` labels work as described in :doc:`downstream-ci`,
but only for a commit with an approved ``CI (fork)`` run:
a label set before the approval takes effect once that run has completed.
The repository's ``Downstream`` workflows can also be started by hand with **Run workflow**,
giving the pull request's number and the level.

A fork's branch name is never matched: its pull request is built against the branches the manifests name,
usually ``develop``. A change that needs branches in several repositories has to come from branches of those
repositories, see :doc:`feature-branches`.
