.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Tutorial
========


Philosophy
----------

Functions, not a framework
~~~~~~~~~~~~~~~~~~~~~~~~~~

ci-infrastructure is a box of parts, not a pipeline you plug into.
Each composite action and each CLI does one thing.
:action:`resolve-deps` resolves, :action:`fetch-deps` downloads, :action:`publish-artifact` uploads.
You call them from your own workflow, in the order you choose.
Only the cross-repo glue is generated: ``trigger-downstream.yml`` and ``cross-repo-trigger.yml``.

Your ``ci.yml`` stays yours
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Test the code the way it needs to be tested.
The ``ci.yml`` of each repo is hand-written.
It picks its own jobs, build steps, test commands and images.
eckit, for example, builds with its own ``./.github/actions/build-eckit``.
An HPC build runs a recipe the repo owns, ``.ci/hpc/build-<toolchain>.sh``.
ci-infrastructure only supplies what goes around the build: dependencies before it, the artifact after it.

Tight boundaries for artifact reuse
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A build is reused only when it is exactly the build you need.
The artifact name carries everything that makes one build differ from another:
the commit, a hash of the dependencies' artifact names, the platform, the compiler, the build type and the options.
A change anywhere upstream changes every name below it.
Scheduling does not count: ``runs-on`` and ``container`` stay out of the name.
So a dependency is downloaded instead of rebuilt only when nothing that matters has changed.
See :doc:`../reference/manifest` for the fields that enter the name.

Don't overdo DRY in a build system
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The sequence cmake, make, ctest looks the same in every repo.
It diverges quickly: compiler flags, optional features, test labels, a Python wheel, an HPC toolchain.
A shared build that absorbs every difference becomes a framework with a hundred inputs.
So the shared pieces stay small.
:action:`cmake-build` covers the plain case, and a repo that needs more writes its own step.
Some repetition across repos is cheaper than one abstraction nobody can change.

Only a fast CI is actually used
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A slow CI gets skipped, batched or ignored.
Several pieces exist only for speed:

- Dependencies come from the S3 artifact store instead of being rebuilt.
- :action:`setup-sccache` shares compiler output between jobs through S3.
- The official images have ci-infrastructure baked in, so no job waits for a pip install.
- Matrix legs run in parallel with ``fail-fast: false``, so one broken leg does not hide the others.

Only a CI that is green by default has diagnostic value
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Red must mean broken.
A check that is red for any other reason teaches people to ignore red.
So a missing decision is *pending*, not failed: :action:`require-label-decision` waits for a label.
Known transient states warn instead of fail, such as an image that is stale right after a merge.
Downstream CI runs only when a pull request asks for it with the ``run-downstream-CI`` label.
