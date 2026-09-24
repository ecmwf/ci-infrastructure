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

Your ``ci.yml`` stays yours
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The people who write a package know best how it has to be built and tested.
Hence, the ``ci.yml`` of each repository is written by hand and can choose its own jobs,
build steps, test commands and container images.
ci-infrastructure only provides what surrounds the build:
the dependencies before it, and the published artifact after it.

Tight boundaries for artifact reuse
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Reusing a build is only safe if it is exactly the build that is needed.
While there is high flexibility in each ``ci.yml``, across repository boundaries the
artifacts have to be precisely defined.
This is done in a ``.ci/manifest.toml``, from which every artifact gets a name:
its commit, a hash of its dependencies' names, and the build configuration.
The fields that enter the name are listed in :doc:`../reference/manifest`.
Thus, a dependency is downloaded instead of rebuilt only if nothing that matters has changed.

Don't overdo DRY in a build system
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

At first sight, the sequence of cmake, make and ctest looks the same in every repository.
In practice it diverges quickly; an incomplete list includes compiler flags,
optional features, test labels, Python wheels and HPC toolchains.
A shared build step that absorbs all of these differences turns into a framework
with a hundred inputs, which nobody dares to change.
The shared pieces are therefore kept deliberately small:
:action:`cmake-build` covers the plain case, and a repository that needs more writes its own step
or overrides parts of it through ``preset`` and ``cmake-args``.
Some repetition across repositories is the cheaper choice.

Only a fast CI is actually used
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A slow CI is skipped, batched, or ignored, and then it no longer protects anything.
Several parts of ci-infrastructure exist only for speed:
(a) dependencies are fetched from the S3 artifact store instead of being rebuilt,
(b) :action:`setup-sccache` shares compiler output between jobs through S3,
(c) the official images have ci-infrastructure baked in, so no job waits for a pip install, and
(d) matrix legs run in parallel with ``fail-fast: false``, so one broken leg does not hide the others.

Downstream CI, finally, rebuilds the dependent repositories and therefore runs only
when a pull request asks for it with the ``run-downstream-CI`` label.

Only a CI that is green by default has diagnostic value
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A red check carries information only if red means broken.
A check that is red for any other reason teaches people to ignore red,
and the next real failure is ignored with it.
In this context, a missing decision is reported as *pending* rather than failed:
:action:`require-label-decision` waits for a label instead of failing the pull request.
In the same spirit, known transient states only warn,
for example an image that is stale in the minutes after a merge.
