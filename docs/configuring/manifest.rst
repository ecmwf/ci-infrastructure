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

There is an additional, optional field ``visibility`` which can be set to ``"private"`` or ``"public"``.
**Setting this field should only happen once both the security and exposure risks are understood.**
A private repo does not need to do any checks of CI approval because it cannot be reached by outside fork code,
so code is always assumed to be approved.
A public repo shows its logs in the downstream CI of its public upstream repos,
this can expose internal logs to the public.
Leaving ``visibility`` undeclared assumes the safer option in both cases, i.e. approval is required for outside code
and logs shall not be exposed.


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

``package`` may list several packages of one repository; they share the other fields.
``when`` limits a dependency to the legs whose fields have one of the listed values,
``unless`` excludes the legs that match it, and ``execution`` stands for the lane, ``runner`` or ``hpc-atos``.
A library the cluster provides as a module, for example, is fetched only on the runners:

.. code:: toml

   [[deps]]
   repo = "ecmwf/stack-dependencies"
   package = ["proj", "qhull"]
   ref = "master"
   compiler-inputs = ["cxx-compiler"]
   unless = { execution = "hpc-atos" }

.. note::

   A package may also declare a dependency that it already receives transitively.
   The two declarations must then agree, for example in ``ref`` and ``compiler-inputs``;
   otherwise :action:`resolve-deps` stops with an error naming both.
   Only the branch has to agree, not its commit, so a push in between is no conflict.

Several packages in one repository
----------------------------------

A repository can publish further packages beside ``[package]``,
each under its own prefix in a ``[packages.<prefix>]`` table with its own ``compiler-inputs`` and ``deps``.
A dependency without ``repo`` names a package of the same repository and is built from the same commit.
Each kind lists what it publishes in ``packages``.
With ``meta = true``, ``[package]`` has no artifact of its own: a dependency on it stands for its ``[[deps]]``,
so a consumer can take the whole set or only the packages it links:

.. code:: toml

   [package]
   name = "stack-deps"
   repo = "ecmwf/stack-dependencies"
   compiler-inputs = ["cxx-compiler"]
   meta = true

   [[deps]]
   package = ["sqlite3", "proj"]

   [packages.sqlite3]
   compiler-inputs = ["cxx-compiler"]

   [packages.proj]
   compiler-inputs = ["cxx-compiler"]
   deps = [{ package = "sqlite3" }]

   [matrix.build]
   packages = ["sqlite3", "proj"]

A kind may publish several packages: one job builds them, each after the packages of the repository it depends on,
and publishes one artifact per package.
Its recipe loops over ``ci_packages``, which lists them in that order, and installs each to ``$CI_INSTALL_ROOT/<name>``.
A kind that publishes a package also waits for the other kinds that publish packages it depends on.
On HPC the job script archives each package after the recipe, so the recipe only installs them.

The build configurations
------------------------

.. code:: toml

   [matrix.build]

   [matrix.build.defaults]
   job-script = "./.ci/build.sh.j2"
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

``job-script`` names the recipe that builds and tests the package.
Together with ``ctest-args`` this is what the generated workflows need
to build the package on behalf of an upstream change, the subject of :doc:`downstream`.
Since the ``ci.yml`` runs the same recipe, the build is described once for both.

The job script
----------------

The recipe that does the actual build and test
is a `Jinja <https://jinja.palletsprojects.com/>`__ template in the repository,
rendered against the leg from the manifrest that runs it into a shell script.
:action:`run-job-script` renders and runs it, in the ``ci.yml`` as well as in the generated workflows,
which use it to rebuild a missing artifact and to build the package in a downstream run.
It is possible to write your own template, but it's recommended
to extend a template shipped with ``ci-infrastructure`` and override only the blocks in which they differ;
``eckit``'s ``.ci/build.sh.j2`` extends one and changes nothing:

.. code:: jinja

   {% extends "ci-infrastructure/cmake-all-lanes.sh.j2" %}

The templates and their blocks are described in :doc:`job-scripts`.

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
           uses: ecmwf/ci-infrastructure/actions/run-job-script@main
           with:
             job-script: ${{ matrix.job-script }}
             matrix-leg: ${{ toJSON(matrix) }}
             package: eckit
             cmake-prefix-path: ${{ steps.deps.outputs.cmake-prefix-path }}
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
- ``_resolved.job-name``: a readable title for the leg

Both jobs run in an official image, which has ``ci-infrastructure`` baked in,
so its actions start without installing it first.
The environment variables point the actions at the artifact store.
``eckit`` additionally mints a GitHub App token for :action:`resolve-deps` and :action:`fetch-deps`,
which raises the API rate limit and gives access to private repositories.
