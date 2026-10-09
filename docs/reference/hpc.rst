.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

HPC builds
==========

This page collects the details behind :doc:`../configuring/hpc`.

The flow of a job
-----------------

::

   hpc-submit runner
     resolve -> fetch deps (S3) -> submit -> ship source, deps, marker -> wait -> fetch install -> publish
                                     |                                     ^
                                     v  troika                             | Finished: SUCCESS/FAILURE
                              compute node: wait for marker -> unpack into $TMPDIR -> build script

The runner drives troika's ``Site`` API from Python.
It submits the job first, which claims the place in the queue,
and then copies the source and the dependencies into a staging directory on the cluster.
A marker file, ``TRANSFER_COMPLETED``, signals the end of the transfer;
the job waits for it, unpacks everything into the node-local ``$TMPDIR`` and builds there.
``CMAKE_PREFIX_PATH`` points at the shipped dependencies, ``LD_LIBRARY_PATH`` at their libraries,
and ``PATH`` at the directories each declares in ``add-to-path``.
The cluster therefore needs neither a GitHub token nor access to S3.

The staging directory belongs to the artifact, not to the run.
Two runs that build the same artifact, e.g. a repository's own CI and a downstream run,
take turns through a lock, ``<staging>.shiplock``,
and the second run finds the transfer of the first one complete.
A lock older than 30 minutes is broken.

The job reports its result by a sentinel line, ``Finished: SUCCESS`` or ``Finished: FAILURE``, in its output.
Only this line decides the outcome;
a queued job, a dropped connection or a timeout means to keep waiting.
While waiting, the step's log shows the job's output as it is written,
and every 30 seconds, until the job runs, its queue state from ``squeue``, e.g.
``submit-wait: job 33846069 PENDING (Priority), est. start 13:45, waiting 12m``.

The build script
----------------

``ci-infrastructure`` wraps the build script with ``#SBATCH --output/--error``,
the variables ``CMAKE_PREFIX_PATH``, ``LD_LIBRARY_PATH``, ``CI_SOURCE_DIR``, ``CI_INSTALL_PREFIX`` and ``CI_INSTALL_ARCHIVE``,
and the sentinel.
The script has to leave a zstd tar of the install tree in ``$CI_INSTALL_ARCHIVE``;
the shared template does this, a hand-written script does it itself:

.. code:: bash

   mkdir -p "$(dirname "$CI_INSTALL_ARCHIVE")"
   tar -cf - -C "$CI_INSTALL_PREFIX" . | zstd -T0 -q -o "$CI_INSTALL_ARCHIVE.part"
   mv "$CI_INSTALL_ARCHIVE.part" "$CI_INSTALL_ARCHIVE"

A hand-written script is named after its toolchain, e.g. ``build-gnu.sh``;
a template is called ``build.sh.j2``, since the leg supplies the toolchain.
``samples/hpc/`` contains one of each.

Artifact names
--------------

``site`` and ``runs-on`` only schedule a job and do not enter the artifact name,
so two legs that differ only in them collide.
``platform`` names the toolchain instead, e.g. ``hpc-atos-gnu``.
Nothing detects a changed module within the same leg:
after changing the toolchain, change ``platform`` as well, or the old artifacts are reused.

Templates
---------

A build script ending in ``.j2`` is rendered with `Jinja <https://jinja.palletsprojects.com/>`__
against its leg. It may use

.. list-table::
   :header-rows: 1

   * - name
     - meaning
   * - ``build_type``, ``modules``, …
     - every leg field, with hyphens as underscores
   * - ``leg['build-type']``
     - the leg as a mapping, spelled as in the manifest
   * - ``artifact_name``
     - the name of the artifact
   * - ``execution``
     - the lane the leg runs in, ``runner`` or ``hpc-atos``
   * - ``| quote``
     - quotes a value as one shell word, as Ansible's filter of that name; ``| sh`` is an older alias

Any other name is an error, also inside a branch that is never taken,
since :doc:`cli/ci-infrastructure-generate` checks the names beforehand.
``leg['x']`` escapes this check.
``| quote`` does not suit a list of flags such as ``ctest-args`` or a ``module`` command,
which must stay several words.
The ``CI_*`` variables and ``CMAKE_PREFIX_PATH`` are shell variables, resolved on the cluster, not template names.

To render a leg without a cluster (see :doc:`cli/ci-infrastructure-hpc`):

.. code:: bash

   ci-infrastructure-hpc render --job-script .ci/hpc/build.sh.j2 \
     --matrix-leg '{"c-compiler": "gcc", "cxx-compiler": "g++", "build-type": "Release", "modules": ["load prgenv/gnu"]}'

The shared template
-------------------

``ci-infrastructure/cmake-atos.sh.j2`` extends the runner template ``cmake-runner.sh.j2``
and consists of these blocks:

.. list-table::
   :header-rows: 1

   * - block
     - content
   * - ``sbatch``
     - ``--qos``, ``--nodes``, ``--ntasks``, ``--cpus-per-task`` and ``--mem`` if set, ``--gres=ssdtmp:``, ``--time``
   * - ``set_environment``
     - empty; for venvs, ``export`` and ``source``, before anything is printed or configured
   * - ``preflight``
     - prints the compiler and CMake versions; extend it with ``{{ super() }}`` for further checks
   * - ``configure``
     - ``cmake --preset``, build type, compilers, rpath, prefix path, install prefix
   * - ``cmake_args``
     - empty, inside ``configure``; every line ends in ``\``
   * - ``build``
     - ``cmake --build "$build" --parallel "$jobs"``
   * - ``test``
     - ``ctest`` with ``ctest_args``, else ``-j "$jobs"``; skipped if ``tests`` is false
   * - ``install``
     - ``cmake --install "$build"``

Afterwards the template archives ``$install_root``.
The blocks share the shell variables ``build`` (the build directory on node-local disk),
``jobs`` (``$SLURM_CPUS_PER_TASK``, else ``$SLURM_NTASKS``),
``install_root`` (``$CI_INSTALL_PREFIX``, or the staged tree after a ``DESTDIR`` install)
and ``gen_flag`` (``-GNinja`` if ninja is available).
In a child template, text outside a block is dropped;
``{% extends %}`` comes first, and a licence header goes into a ``{# #}`` comment.

A leg may omit these fields:

.. list-table::
   :header-rows: 1

   * - field
     - default
   * - ``qos``
     - ``nf``
   * - ``nodes``
     - ``1``
   * - ``time``
     - ``01:00:00``
   * - ``ntasks``
     - ``8``
   * - ``cpus-per-task``, ``mem``
     - unset
   * - ``ssdtmp``
     - ``20G``
   * - ``tests``
     - ``true``
   * - ``ctest-args``, ``options``
     - ``""``
   * - ``python-version``
     - ``""``; a string such as ``"3.11"``, never a number (see :doc:`../configuring/job-scripts`)
   * - ``c-compiler-binary``, ``cxx-compiler-binary``
     - ``c-compiler``, ``cxx-compiler``
   * - ``fortran-compiler-binary``
     - ``fortran-compiler``; without it ``""``, i.e. no Fortran

A template defaults a field it reads through ``| default(...)``;
``qos`` to ``ssdtmp`` are defaulted that way in ``cmake-atos.sh.j2``, the others in Python.

Any other name a template reads must be a field of every leg that uses it,
or set by the template itself outside its blocks with ``{% set %}``, ``{% import %}`` or a macro.
Rendering checks this before it runs, as the workflow generator does,
so a recipe that reads an undeclared field fails in the pull request,
even in a branch the leg never takes.

``configure`` uses the preset named by ``options``, or ``ci`` if it is empty.
The feature flags thus live in the package's ``CMakePresets.json``,
which the runner build can use as well; an option preset inherits ``ci``.
Presets need CMake 3.21.

.. code:: json

   {
     "version": 3,
     "configurePresets": [
       {"name": "ci", "cacheVariables": {"ENABLE_TESTS": "ON"}},
       {"name": "with-geo", "inherits": "ci", "cacheVariables": {"ENABLE_GEOGRAPHY": "ON"}}
     ]
   }

Every package loads the template from ``ci-infrastructure`` ``@main``.
A changed template must therefore rebuild every HPC artifact,
which is why the artifact names carry ``-hpcv<N>``, with ``N`` = ``HPC_TEMPLATE_VERSION`` (no segment for 0).
A test fails until a changed template comes with a new version.

Cluster configuration
---------------------

- ``site`` names a troika site from ``src/ci_infrastructure/hpc/troika-config.yml``.
  Only SLURM sites can build, since the flow needs a scheduler to submit to and poll.
- ``vars.HPC_CI_REMOTE_WORK_DIR`` is the base directory on the cluster, recommended ``$SCRATCH/github-ci``.
  It is expanded on the cluster, in a login shell, and must yield an absolute path.
- ``vars.HPC_CI_WORK_DIR`` is a scratch directory on the runner, by default ``$RUNNER_TEMP``.
- ``secrets.HPC_CI_SSH_USER`` optionally sets the user on the cluster.

On ECMWF's Atos, staging and install go to ``$SCRATCH``, shared Lustre with a 30-day purge,
and the build to ``$TMPDIR``, the node-local SSD, whose size ``ssdtmp`` requests.
The marker, the tarballs and the job output must be on the shared filesystem.

Restarts and cleanup
--------------------

A job is named ``ci-<artifact>``.
A re-run therefore skips an artifact that exists,
reattaches to a job of the same name that is still in the queue,
and submits a new one otherwise.
Cancelling the GitHub job cancels the SLURM job.

A new submission clears the artifact's staging directory.
``hpc-nightly-cleanup.yml`` removes older trees under ``HPC_CI_REMOTE_WORK_DIR``;
dispatched with ``dryrun: true`` it only lists them.

Moving directories
------------------

:action:`push-hpc-tree`, :action:`fetch-hpc-tree` and :action:`remove-hpc-tree`
copy a directory to the cluster, back to the runner, or remove it there.
They need neither a scheduler nor S3.
``remote-dir`` must be on the shared filesystem and is expanded on the cluster,
so quote a ``$SCRATCH/…`` path in a shell.

.. code:: yaml

   - uses: ecmwf/ci-infrastructure/actions/push-hpc-tree@main
     id: push
     with:
       site: hpc-batch
       troika-user: ${{ secrets.HPC_CI_SSH_USER }}
       local-dir: ./inputs
       remote-dir: ${{ env.OUTPUT_DIR }}/inputs
   # ... the job ...
   - uses: ecmwf/ci-infrastructure/actions/remove-hpc-tree@main
     with:
       site: hpc-batch
       troika-user: ${{ secrets.HPC_CI_SSH_USER }}
       remote-dir: ${{ steps.push.outputs.remote-dir }}

:action:`remove-hpc-tree` goes last and without ``if:``,
so that a failed job keeps its trees for debugging until the nightly cleanup.
