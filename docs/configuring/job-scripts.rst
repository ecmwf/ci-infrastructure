.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Build with a recipe on runners and HPC
======================================

A runner leg can name a ``job-script`` just like an HPC leg.
On a runner, :action:`run-job-script` renders it and runs it in the job's container;
on HPC, :action:`build-on-hpc` submits it as a SLURM job (see :doc:`hpc`).
Either way the recipe renders to one shell script,
and ``ci-infrastructure-render`` prints that same script for you to run by hand.

Shared Jinja templates
----------------------

``ci-infrastructure`` ships Jinja templates that a recipe extends.
So far they build one CMake project:

.. list-table::
   :header-rows: 1

   * - template
     - for
   * - ``ci-infrastructure/cmake-runner.sh.j2``
     - a GitHub runner: configure, build, test and install one CMake project
   * - ``ci-infrastructure/cmake-atos.sh.j2``
     - HPC: extends the runner template and adds the ``#SBATCH`` header, the modules,
       the node-local build tree and the install archive
   * - ``ci-infrastructure/cmake-all-lanes.sh.j2``
     - every lane: extends one of the two above, chosen by the leg's ``execution``

The HPC template fills three blocks that are empty or different on a runner:
``header`` (holding ``sbatch`` and the modules), ``setup`` (``build``, ``jobs``, ``install_root``)
and ``publish`` (the archive).
All other blocks are shared; :doc:`../reference/hpc` lists them.

One recipe for all lanes
------------------------

Extend ``cmake-all-lanes.sh.j2``, and one ``.ci/build.sh.j2`` serves the runner and the HPC legs.
It extends the template of the leg's lane, so an override is written once,
and ``execution`` tells the lanes apart where they differ:

.. code:: jinja

   {% extends "ci-infrastructure/cmake-all-lanes.sh.j2" %}
   {% block test %}
   for v in 0 1; do
     ECCODES_ECKIT_GEO=$v ctest --test-dir "$build" --output-on-failure -j "$jobs"
   done
   {% endblock %}
   {% block install %}
   {% if execution == "hpc-atos" %}
   DESTDIR="${TMPDIR:-/tmp}/stage" cmake --install "$build"
   install_root="${TMPDIR:-/tmp}/stage$CI_INSTALL_PREFIX"
   {% else %}
   {{ super() }}
   {% endif %}
   {% endblock %}

Point the legs of every kind at it.
The runner kind drops its ``action`` and ``ctest``, since the recipe's ``test`` block runs the tests:

.. code:: toml

   [matrix.build.defaults]
   job-script = "./.ci/build.sh.j2"

   [matrix.build-hpc.defaults]
   job-script = "./.ci/build.sh.j2"

A recipe may also extend one lane's template directly, ``cmake-runner.sh.j2`` or ``cmake-atos.sh.j2``,
and serve only that lane.

Runner legs name their compilers in ``c-compiler``, ``cxx-compiler`` and ``fortran-compiler``;
the recipe reads them as ``cc``, ``cxx`` and ``fc``.
Name the C compiler too: left out, CMake picks the image's default ``cc``.

Reproduce a leg locally
-----------------------

List the legs by the job titles the Actions UI shows, then render one:

.. code:: console

   $ ci-infrastructure-render --list
   eccodes/build (ubuntu-24.04, g++-13, default)
   eccodes/build (ubuntu-24.04, g++-13, eckit-geo)
   eccodes/build-hpc (hpc-atos-gnu, g++-8, eckit-geo)
   $ ci-infrastructure-render --leg 'build (ubuntu-24.04, g++-13, eckit-geo)' -o ci-job.sh

The script exports its environment with defaults under the current directory;
set any of these to override it:

.. list-table::
   :header-rows: 1

   * - variable
     - default
   * - ``CI_SOURCE_DIR``
     - ``$PWD``
   * - ``CI_BUILD_DIR``
     - ``$PWD/_ci/build`` (runner lane only)
   * - ``CI_INSTALL_PREFIX``
     - ``$PWD/_ci/install``
   * - ``CMAKE_PREFIX_PATH``
     - empty
   * - ``CI_INSTALL_ARCHIVE``
     - ``$CI_INSTALL_PREFIX.install.tar.zst`` (HPC lane only)

Run it in the leg's container, which the script names in a comment:

.. code:: console

   $ docker run --rm -v "$PWD":/src -w /src \
       -e "CMAKE_PREFIX_PATH=/deps/ecbuild;/deps/eckit" -v "$HOME/deps":/deps \
       eccr.ecmwf.int/public-ci-images/ubuntu24.04-gcc13-gfortran13:latest bash ci-job.sh

CI fetches the dependencies from the artifact store;
locally you provide them, and ``CMAKE_PREFIX_PATH`` (``;``-separated) says where.
An HPC leg's script also runs on an interactive node:
to bash the ``#SBATCH`` lines are comments, and the ``module`` lines load the toolchain.

Migrate from a build action
---------------------------

:action:`cmake-build` and the ``.github/actions/build-<package>`` wrappers around it are deprecated.
To move a package over:

#. Write ``.ci/build.sh.j2``, extending ``ci-infrastructure/cmake-runner.sh.j2``.
   Move what the build action did differently, such as extra ``-D`` flags, into its blocks.
#. Set ``job-script = "./.ci/build.sh.j2"`` in the runner kind's defaults,
   and drop its ``action``, ``forwarded-inputs`` and ``ctest``.
#. In ``ci.yml``, build with :action:`run-job-script` instead of the package's action, then delete the action.
#. Regenerate the workflows with ``ci-infrastructure-generate``.
