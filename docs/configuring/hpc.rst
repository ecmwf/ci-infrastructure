.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Add an HPC build
================

An HPC build is a matrix kind with ``execution = "hpc-atos"``.
It builds and tests the package inside a SLURM job on ECMWF's HPC,
while dependencies, artifact names and publishing work as for any other kind.

How it works
------------

The job itself runs on a small runner group, ``hpc-submit`` (see :doc:`../reference/runners`).
This runner downloads the dependencies from the artifact store, ships them together with the source to the HPC,
and submits the build script through `troika <https://github.com/ecmwf/troika>`__.
Once the job has finished, it copies the install tree back and uploads it to the artifact store.
The compute node itself needs neither a GitHub token nor access to the artifact store.

The manifest
------------

``eccodes`` declares its HPC kind next to its ``build`` kind:

.. code:: toml

   [matrix.build-hpc]
   execution = "hpc-atos"
   triggers = ["upstream-change", "rebuild-request"]
   container-credentials = true

   [matrix.build-hpc.defaults]
   job-script = "./.ci/hpc/build.sh.j2"
   platform = "hpc-atos-gnu"
   cxx-compiler = "g++-8"
   fortran-compiler = "gfortran-8"
   build-type = "RelWithDebInfo"
   runs-on = "hpc-submit"
   container = "eccr.ecmwf.int/private-ci-images/ubuntu24.04-internal-tools:latest"
   site = "hpc-batch"
   modules = ["load prgenv/gnu", "unload gcc", "load gcc/old", "load cmake", "load ninja"]
   c-compiler-binary = "gcc"
   cxx-compiler-binary = "g++"
   fortran-compiler-binary = "gfortran"
   time = "01:30:00"

   [[matrix.build-hpc.include]]
   options = ""

   [[matrix.build-hpc.include]]
   options = "eckit-geo"

``job-script`` points to the build script, and every field of a leg is passed to it.
It is a leg field, so a leg may bring its own build script.
A build on the HPC usually needs control over the exact modules, the job's resources and the like,
so these are leg fields such as ``modules`` and ``time`` above.
``[matrix.build-hpc.defaults]`` holds the fields that all legs share,
and a leg's own field wins.
The two legs of ``eccodes`` therefore differ only in ``options``,
which selects a preset from the package's ``CMakePresets.json``.
``platform`` names the toolchain as part of the artifact name, e.g. ``hpc-atos-gnu``,
since it differs from the runners' images.
``cxx-compiler`` and ``fortran-compiler`` identify the build;
the module ``gcc/old`` provides the compilers without a version suffix,
so the ``*-compiler-binary`` fields name the binaries the build script calls.

The build script
----------------

It is possible to write specific build scripts for every leg, or for sets of legs
to account for the quirks of a compiler toolchain.
However, where possible it is preferred to use a uniform template and fill it with the information from the manifest
as in the example above.

In the most extreme case one writes the shell script with the appropriate
SLURM directives and module commands for every leg and only declares the minimum required information in the manifest.
Such a script must also archive the install tree itself (see :doc:`../reference/hpc`).

The above manifest would then require two hand-written shell scripts and look like:

.. code:: toml

   [matrix.build-hpc]
   execution = "hpc-atos"
   triggers = ["upstream-change", "rebuild-request"]
   container-credentials = true

   [matrix.build-hpc.defaults]
   platform = "hpc-atos-gnu"
   cxx-compiler = "g++-8"
   fortran-compiler = "gfortran-8"
   build-type = "RelWithDebInfo"
   runs-on = "hpc-submit"
   container = "eccr.ecmwf.int/private-ci-images/ubuntu24.04-internal-tools:latest"
   site = "hpc-batch"

   [[matrix.build-hpc.include]]
   job-script = "./.ci/hpc/build-default.sh"
   options = ""

   [[matrix.build-hpc.include]]
   job-script = "./.ci/hpc/build-eckit-geo.sh"
   options = "eckit-geo"

A build script whose name ends in ``.j2`` is a `Jinja <https://jinja.palletsprojects.com/>`__ template,
rendered against the leg that runs it.
The first manifest works this way.

A package may write its own template,
but most packages are similar enough to extend the shared one:

.. code:: jinja

   {% extends "ci-infrastructure/cmake-atos.sh.j2" %}

The template consists of blocks, ``sbatch``, ``preflight``, ``configure``, ``build``, ``test`` and ``install``,
and a package overrides only those in which it differs;
``{{ super() }}`` keeps the shared content.
It extends the runner template, which runner legs use (see :doc:`job-scripts`).

``eccodes``, for example, downloads its test data before running the tests,
so that a failed download does not show up as dozens of unrelated test failures:

.. code:: jinja

   {% extends "ci-infrastructure/cmake-atos.sh.j2" %}
   {% block test %}
   ctest --test-dir "$build" --output-on-failure -L download_data -j 6
   ctest --test-dir "$build" --output-on-failure -LE download_data -j "$jobs"
   {% endblock %}

To see what a leg produces, without a cluster, use :doc:`../reference/cli/ci-infrastructure-hpc`:

.. code:: bash

   ci-infrastructure-hpc render --job-script .ci/hpc/build.sh.j2 \
     --matrix-leg '{"c-compiler": "gcc", "cxx-compiler": "g++", "build-type": "Release", "modules": ["load prgenv/gnu"]}'

The ``ci.yml`` runs :action:`build-on-hpc` in place of the build step,
and its log shows every rendered job script before it is submitted.
The blocks, the fields a template may use and their defaults are in :doc:`../reference/hpc`.
