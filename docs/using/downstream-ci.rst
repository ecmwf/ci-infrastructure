.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Running the downstream CI
=========================

.. note::

   This page is opinionated, open for debate, and likely to change (as of 2026-09-30).

Every pull request can run two kinds of CI.
The *own CI* builds and tests the repository itself and is the minimal test that always runs
(see :doc:`best practices <default-branch>`).
The *downstream CI* additionally builds and tests its consumers, i.e. the repositories that depend on it.
For ``eckit``, these are e.g. ``eccodes`` and ``multio``.
How the consumers are declared is described in :doc:`../configuring/downstream`.

The downstream CI answers one very specific question, namely:
do my changes break downstream code?

Until now, the downstream CI ran unconditionally, which put a large strain on our resources, often without any benefit.
Moreover, it meant that the CI of every pull request ran for several hours,
which was an incentive to skip pull requests and to push directly to the default branch.
This, in turn, broke downstream packages **with errors that the ordinary tests alone would have caught**.
Hence, the downstream CI partly caused the very problems it was created to prevent.

The new system is therefore designed to **always** run a repository's own CI,
while the downstream CI runs only where it adds value.
In the following we describe how the downstream CI is controlled
and discuss typical cases in which running it does, or does not, make sense.

Controlling the downstream CI
+++++++++++++++++++++++++++++

The downstream CI runs only for a pull request, and only if the pull request asks for it with one of the following labels:

- ``run-downstream-ci:all`` builds and tests every consumer.
- ``run-downstream-ci:<n>`` builds and tests the consumers up to level ``n``.
  In the figures below, ``eccodes`` is at level 1 and ``multio`` at level 2, as seen from ``eckit``.

If you are sure that a PR cannot break a consumer,
e.g. a change to the documentation, to the CI, or to the tests only,
then it is possible to explicitly opt out of running via the label ``downstream-ci-not-needed``.
The action :action:`require-label-decision` ensures that this decision is always taken explicitly and is never forgotten,
by enforcing that a pull request has exactly one of the run or the opt-out label to be merged.

The downstream CI starts once the ``CI`` of the pull request has completed successfully,
hence the label has to be on the pull request by then.
Adding it later is not a problem, since this starts a fresh ``CI`` run, which then triggers the downstream CI.
We also note that the label stays on the pull request, so that every further push runs the downstream CI again;
it is therefore cheapest to add the label once the pull request is ready for review.
The results are reported as the statuses ``downstream/runner`` and ``downstream/hpc-atos`` on the pull request.

It has to be emphasized that the downstream CI does **not run** on the default branch,
that is, a push to ``develop``, e.g. a merge, does not fan out to the consumers.
This deliberate change of behaviour is explained with the last two figures below.

Discussion of different applications
++++++++++++++++++++++++++++++++++++

In this section, we discuss four typical situations with the same chain of four repositories,
``ecbuild``, ``eckit``, ``eccodes``, and ``multio``, where each arrow points from a repository to the one it depends on.
The figures encode the branch of every repository by its color, blue for ``develop`` and orange for a feature branch.
A box below a repository is a test:
its own tests first, and the tests of its downstream consumers in the black box.
Each test box is split into one cell per repository that the test is built from,
and each cell is colored by the branch of that repository.
Thus, two test runs are exactly the same test, including their dependencies, if and only if their cells have the same colors.

A change on a single branch
~~~~~~~~~~~~~~~~~~~~~~~~~~~

We first consider a change of ``eckit``'s API on the branch ``api-change``.
In their own CI, ``eccodes`` and ``multio`` build against ``eckit``'s ``develop`` and hence never see this change.
Only ``eckit``'s downstream CI builds them against ``api-change``,
which makes it the one place where the question "do my changes break downstream code?" is actually answered.
This is the case the downstream CI is made for,
and it is worth running for every change that can reach a consumer,
e.g. a change to the public API or ABI, to the behavior, or to the exported CMake configuration.

.. figure:: downstream-ci-branch.svg
   :alt: eckit is on api-change; only its downstream CI tests eccodes and multio against the change.
   :width: 100%

   ``eckit`` is on ``api-change``; only its downstream CI tests ``eccodes`` and ``multio`` against the change.
   Note the colors of the ``eccodes`` tests:
   in ``eckit``'s downstream CI they are blue, orange, blue,
   i.e. built from ``ecbuild``'s ``develop``, ``eckit``'s feature branch, and ``eccodes``' ``develop``.
   In ``eccodes``' own CI, the same tests are all blue, i.e. built against ``eckit``'s ``develop``.

A coordinated change on a sync branch
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Next, ``eccodes`` adapts to the new API on a branch with the same name, ``sync-branch/api-change`` (see :doc:`feature-branches`).
At first glance, one could expect that the downstream CI of both pull requests is needed.
However, ``eccodes``' own CI now builds against ``eckit``'s branch as well,
so that ``eckit``'s downstream CI runs exactly the tests that ``eccodes``' own and downstream CI run anyway.
Running it from both pull requests therefore only doubles the cost.
It suffices to request the downstream CI on the pull request of the repository furthest downstream on the sync branch, here ``eccodes``.

.. figure:: downstream-ci-sync.svg
   :alt: eckit and eccodes are on sync-branch/api-change; eckit's downstream CI repeats the tests of eccodes' own and downstream CI.
   :width: 100%

   ``eckit`` and ``eccodes`` are on ``sync-branch/api-change``; ``eckit``'s downstream CI repeats the tests of ``eccodes``' own and downstream CI.

The default branch
~~~~~~~~~~~~~~~~~~

If every repository is on ``develop``, all cells are blue.
Every downstream test is then the own test of a consumer, repeated,
which that consumer's CI has already run on its own ``develop``.
For a chain of :math:`n` repositories, these are :math:`n(n-1)/2` test runs, i.e. their number scales as :math:`\mathcal{O}(n^2)`,
and none of them adds any information.
This is why the downstream CI does not run on the default branch.

.. figure:: downstream-ci.svg
   :alt: All repositories are on develop; every downstream test repeats the own test of a consumer.
   :width: 100%

   All repositories are on ``develop``; every downstream test repeats the own test of a consumer.

The capacity saved in this way is better spent on tests that do add information,
e.g. expensive nightly tests, such as valgrind and sanitizers, that each repository runs on its own ``develop``.
Their number scales as :math:`\mathcal{O}(n)`, with :math:`n` being the number of repositories.
Note that ``ecbuild``, a CMake build system without compiled code of its own, has nothing for them to check.

.. figure:: downstream-ci-nightly.svg
   :alt: The capacity of the repeated downstream tests is spent on nightly valgrind and sanitizer runs instead.
   :width: 100%

   The capacity of the repeated downstream tests is spent on nightly valgrind and sanitizer runs instead.


Closing remarks
+++++++++++++++

In conclusion, the new system rests on a few simple rules:

- Every change goes through a pull request, whose own CI always runs (see :doc:`default-branch`).
- Every pull request decides on the downstream CI with exactly one label:
  it is run when the change can reach a consumer, and skipped when it cannot.
- A coordinated change goes on a sync branch;
  the downstream CI runs from the repository furthest downstream on it,
  and the pull requests are merged from upstream to downstream.
- The default branch runs no downstream CI;
  its capacity goes to expensive tests of each repository instead.

This page is open for debate,
and we welcome every view in an `issue in ci-infrastructure <https://github.com/ecmwf/ci-infrastructure/issues>`__.
