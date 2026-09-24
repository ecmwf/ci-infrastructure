.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Tutorial
========


Philosophy
----------

The ECMWF software stack is a graph of packages that build on each other.
A change in eckit can break eccodes, and a change in ecbuild can break both.
Testing a package in isolation therefore misses exactly the failures that matter most,
while rebuilding the whole graph for every commit is far too slow to be useful.
ci-infrastructure sits between these two extremes.
The following principles shaped its design, and they explain most of the decisions
that may otherwise look arbitrary.

Functions, not a framework
~~~~~~~~~~~~~~~~~~~~~~~~~~

ci-infrastructure is a set of building blocks rather than a pipeline that a repository plugs into.
Each composite action and each command-line tool does one thing:
:action:`resolve-deps` resolves the dependency graph, :action:`fetch-deps` downloads the
resolved artifacts, and :action:`publish-artifact` uploads the result.
They are called from the repository's own workflow, in the order it chooses.
Only the cross-repository glue, ``trigger-downstream.yml`` and ``cross-repo-trigger.yml``,
is generated, because it has to agree across all repositories in the graph.

Your ``ci.yml`` stays yours
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The people who write a package know best how it has to be tested.
Hence, the ``ci.yml`` of each repository is written by hand and chooses its own jobs,
build steps, test commands and container images.
eckit, for example, builds with its own ``./.github/actions/build-eckit``,
and an HPC build runs a recipe the repository owns, ``.ci/hpc/build-<toolchain>.sh``.
ci-infrastructure only provides what surrounds the build:
the dependencies before it, and the published artifact after it.

Tight boundaries for artifact reuse
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Reusing a build is only safe if it is exactly the build that is needed.
The artifact name therefore contains everything that distinguishes one build from another:
the commit, a hash of the dependencies' artifact names, the platform, the compiler,
the build type and the options.
Since the dependencies enter the name through their own names, a change anywhere upstream
changes every name below it, in the same way as a Merkle tree.
Scheduling, on the other hand, does not count:
``runs-on`` and ``container`` decide *where* a build runs, not *what* it produces,
and stay out of the name.
Thus, a dependency is downloaded instead of rebuilt only if nothing that matters has changed.
The fields that enter the name are listed in :doc:`../reference/manifest`.

Don't overdo DRY in a build system
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

At first sight, the sequence of cmake, make and ctest looks the same in every repository.
In practice it diverges quickly; an incomplete list includes compiler flags,
optional features, test labels, Python wheels and HPC toolchains.
A shared build step that absorbs all of these differences turns into a framework
with a hundred inputs, which nobody dares to change.
The shared pieces are therefore kept deliberately small:
:action:`cmake-build` covers the plain case, and a repository that needs more writes its own step.
Some repetition across repositories is the cheaper choice.

Only a fast CI is actually used
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A slow CI is skipped, batched, or ignored, and then it no longer protects anything.
Several parts of ci-infrastructure exist only for speed:
(a) dependencies are fetched from the S3 artifact store instead of being rebuilt,
(b) :action:`setup-sccache` shares compiler output between jobs through S3,
(c) the official images have ci-infrastructure baked in, so no job waits for a pip install, and
(d) matrix legs run in parallel with ``fail-fast: false``, so one broken leg does not hide the others.

Only a CI that is green by default has diagnostic value
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A red check carries information only if red means broken.
A check that is red for any other reason teaches people to ignore red,
and the next real failure is ignored with it.
In this context, a missing decision is reported as *pending* rather than failed:
:action:`require-label-decision` waits for a label instead of failing the pull request.
In the same spirit, known transient states only warn,
for example an image that is stale in the minutes after a merge.
Downstream CI, finally, runs only when a pull request asks for it
with the ``run-downstream-CI`` label.
