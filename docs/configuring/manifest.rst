.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

The Manifest
============

A repository that uses other ECMWF packages, or is used by them, describes itself in ``.ci/manifest.toml``.
The manifest is the contract across repository boundaries:
it states what the package is, what it depends on, and in which configurations it is built.
From it, :action:`resolve-deps` computes which artifacts a build needs,
and :doc:`ci-infrastructure-generate <../reference/cli/ci-infrastructure-generate>` writes the workflows for the downstream CI.
Both validate the manifest, reject unknown keys
and enforce constraints, such as the dependencies being a directed, acyclic graph.
The formal specification and complete list of fields is in :doc:`../reference/manifest`.
This page builds up ``eckit``'s manifest step by step.

The package
-----------

.. code:: toml

   [package]
   name = "eckit"
   repo = "ecmwf/eckit"
   visibility = "public"
   compiler-inputs = ["cxx-compiler"]

``name`` identifies the package in the graph.
An optional ``prefix`` starts the name of every artifact the package publishes,
and consumers use it to refer to the package; if omitted, it is ``name``.
Since it becomes part of file names and object-store keys,
it may contain only letters, digits, ``-`` and ``_``.
``compiler-inputs`` lists the fields of a build configuration that name the compilers.
It is a list because a package may compile several languages, e.g. both C++ and Fortran.
A package that compiles nothing, such as ``ecbuild``, declares ``compiler-inputs = []``.

The dependencies
----------------

.. code:: toml

   [[deps]]
   repo = "ecmwf/ecbuild"
   package = "ecbuild"
   ref = "develop"
   compiler-inputs = []

   [[deps]]
   repo = "ecmwf/stack-dependencies"
   package = "stack-deps"
   ref = "master"
   compiler-inputs = ["cxx-compiler"]

Every ``[[deps]]`` entry names an upstream repository and the ``prefix`` of its package.
``ref`` is the branch a build normally uses;
a coordinated change on a ``sync-branch/`` or ``feature/`` branch uses the upstream's branch of the same name instead,
see :doc:`../using/feature-branches`.

``compiler-inputs`` must match the upstream's own ``[package].compiler-inputs``,
so that ``eckit`` asks for the ``stack-dependencies`` build made with the same compiler as its own.
Dependencies are transitive: ``eckit``'s consumers receive ``ecbuild`` and ``stack-dependencies`` without declaring them again.

.. note::

   A package may also declare a dependency that it already receives transitively.
   The two declarations must then agree, for example in ``ref`` and ``compiler-inputs``;
   otherwise :action:`resolve-deps` stops with an error naming both.
   Only the branch has to agree, not its commit, so a push in between is no conflict.

The build configurations
------------------------

.. code:: toml

   [matrix.build]
   action = "./.github/actions/build-eckit"
   forwarded-inputs = ["c-compiler", "cxx-compiler", "build-type"]
   forwarded-deps-outputs = ["cmake-prefix-path"]
   ctest = true

   [matrix.build.defaults]
   build-type = "RelWithDebInfo"
   runs-on = "arc-runner-very-large"
   ctest-args = '-j "$(nproc)"'

   [[matrix.build.include]]
   cxx-compiler = "g++-13"
   c-compiler = "gcc-13"
   container = "eccr.ecmwf.int/public-ci-images/ubuntu24.04-gcc13-gfortran13:latest"
   platform = "ubuntu-24.04"

   [[matrix.build.include]]
   cxx-compiler = "clang++-18"
   c-compiler = "clang-18"
   container = "eccr.ecmwf.int/public-ci-images/ubuntu24.04-clang18:latest"
   platform = "ubuntu-24.04"

A ``[matrix.<kind>]`` describes one kind of job, here ``build``,
and each ``[[matrix.<kind>.include]]`` adds one configuration of it, a *leg*.
``[matrix.<kind>.defaults]`` holds the fields every leg shares, and a leg's own field wins.
The kind's fields describe how the job runs and are checked against a fixed schema,
whereas everything that may differ between legs is a leg field.
A leg is otherwise free-form, with one required field, ``platform``,
which names the binary-compatibility class.
It is the developer's responsibility to specify matching platforms.
The ``ci.yml`` receives these legs through :action:`resolve-deps` as the matrix of its build job,
each enriched with the resolved dependencies and the name of its own artifact.

Not every field has the same weight.
``platform``, ``build-type``, ``python-version``, ``options`` and the ``compiler-inputs`` fields
distinguish one build from another and enter the artifact name,
whereas ``runs-on`` and ``container`` only decide where a job runs.
Moving a leg to a larger runner therefore reuses its artifacts,
while changing its compiler builds everything below it anew.
That is why ``platform`` is not automatically deduced from ``runs-on`` and ``container``.

``action`` names the repository's own build step,
and ``forwarded-inputs`` and ``forwarded-deps-outputs`` state what it receives.
Together with ``ctest`` and ``ctest-args`` this is what the generated workflows need
to build the package on behalf of an upstream change, the subject of :doc:`downstream`.
Since the ``ci.yml`` calls the same action, the build is described once for both.

The build action
----------------

A package declares how to build itself in an action;
how it is tested follows from ``ctest`` and ``ctest-args`` in the manifest.
The action is also the entry point for other repositories:
the generated workflows call it to rebuild a missing artifact
and to build the package in a downstream run.
The action is an ordinary composite action in the repository, ``.github/actions/build-eckit/action.yml``:

.. code:: yaml

   name: Build eckit from source
   description: Builds and installs the checked-out eckit.

   inputs:
     cmake-prefix-path:
       required: true
     c-compiler:
       required: true
     cxx-compiler:
       required: true
     build-type:
       required: true

   outputs:
     install-path:
       value: ${{ steps.build.outputs.install-path }}
     build-dir:
       value: ${{ steps.build.outputs.build-dir }}

   runs:
     using: composite
     steps:
       - id: build
         uses: ecmwf/ci-infrastructure/actions/cmake-build@main
         with:
           package: eckit
           cmake-prefix-path: ${{ inputs.cmake-prefix-path }}
           c-compiler: ${{ inputs.c-compiler }}
           cxx-compiler: ${{ inputs.cxx-compiler }}
           build-type: ${{ inputs.build-type }}

Its interface follows from the manifest.
The inputs are the entries of ``forwarded-deps-outputs``, filled by :action:`fetch-deps`,
and the leg fields named in ``forwarded-inputs``.
The outputs are fixed:
``install-path`` is the tree that :action:`publish-artifact` uploads,
and ``build-dir`` is where ctest runs when ``ctest = true``.
How the action gets there is up to the repository.
``eckit`` delegates everything to :action:`cmake-build`;
a package with a more particular build replaces that step with its own,
as long as it provides ``install-path``, and ``build-dir`` if it runs ctest.
Often a small addition is enough.
``ecflow``, for example, passes its Boost options to :action:`cmake-build` as ``cmake-args``:

.. code:: yaml

   cmake-args: |
     -DENABLE_CONFIG_MODE_BOOST=OFF
     -DENABLE_STATIC_BOOST_LIBS=OFF
     -DBOOST_ROOT=${{ inputs.boost-root || '/usr' }}

The Boost location comes from the manifest:
the macOS legs set a ``boost-root`` field, ``forwarded-inputs`` passes it to the action,
and the Linux legs, which set nothing, get the images' ``/usr``.
A setting that differs between legs thus stays next to the legs it belongs to,
instead of being detected at run time.
The complete action is
`build-ecflow <https://github.com/ecmwf/ecflow/blob/develop/.github/actions/build-ecflow/action.yml>`__.

From the manifest to the ci.yml
-------------------------------

The ``ci.yml`` does not read the manifest itself.
A ``resolve`` job hands it to :action:`resolve-deps`,
which returns the legs of each requested kind as a JSON matrix,
and the build job runs once per leg.
Condensed from ``eckit``'s ``ci.yml``:

.. code:: yaml

   env:
     ARTIFACT_S3_ENDPOINT: ${{ secrets.ARTIFACT_S3_ENDPOINT }}
     ARTIFACT_S3_BUCKET: ${{ secrets.ARTIFACT_S3_BUCKET }}
     AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
     AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}

   jobs:
     resolve:
       runs-on: arc-runner-normal
       container:
         image: eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest
       outputs:
         build-matrix: ${{ steps.r.outputs.matrix-build }}
       steps:
         - uses: actions/checkout@v6
         - id: r
           uses: ecmwf/ci-infrastructure/actions/resolve-deps@main
           with:
             matrix: build

     build:
       needs: resolve
       name: build+test (${{ matrix._resolved['job-name'] }})
       runs-on: ${{ matrix['runs-on'] }}
       container:
         image: ${{ matrix.container }}
       strategy:
         fail-fast: false
         matrix: ${{ fromJSON(needs.resolve.outputs.build-matrix) }}
       steps:
         - uses: actions/checkout@v6
         - id: deps
           uses: ecmwf/ci-infrastructure/actions/fetch-deps@main
           with:
             deps-json: ${{ toJSON(matrix._resolved.deps) }}
         - id: build
           uses: ./.github/actions/build-eckit
           with:
             cmake-prefix-path: ${{ steps.deps.outputs.cmake-prefix-path }}
             c-compiler: ${{ matrix.c-compiler }}
             cxx-compiler: ${{ matrix.cxx-compiler }}
             build-type: ${{ matrix.build-type }}
         - run: ctest --test-dir "${{ steps.build.outputs.build-dir }}" --output-on-failure ${{ matrix._resolved['ctest-args'] }}
         - uses: ecmwf/ci-infrastructure/actions/publish-artifact@main
           with:
             install-path: ${{ steps.build.outputs.install-path }}
             artifact-name: ${{ matrix._resolved.own-artifact-name }}

Each leg arrives with its own fields unchanged,
so ``matrix.runs-on``, ``matrix.container`` and ``matrix.cxx-compiler``
are what the manifest declares, with ``defaults`` filled in.
In addition, :action:`resolve-deps` attaches a ``_resolved`` object to every leg:

- ``_resolved.deps``: the resolved dependencies, which :action:`fetch-deps` downloads
- ``_resolved.own-artifact-name``: the name under which :action:`publish-artifact` stores the result
- ``_resolved.ctest-args``: the leg's ``ctest-args``, so that the ``ci.yml`` and the downstream runs test alike
- ``_resolved.job-name``: a readable title for the leg

Both jobs run in an official image, which has ``ci-infrastructure`` baked in,
so its actions start without installing it first.
The environment variables point the actions at the artifact store.
``eckit`` additionally mints a GitHub App token for :action:`resolve-deps` and :action:`fetch-deps`,
which raises the API rate limit and gives access to private repositories.
