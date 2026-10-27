.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Build with a recipe on runners and HPC
======================================

A runner leg can name a ``job-script`` just like an HPC leg.
Both the runners and the HPC execute a jinja script.
Usually these scripts are the same, with the HPC only adding ``module load`` and ``#SBATCH``
commands.
On a runner, :action:`run-job-script` renders it with the information from the manifest
and runs it in the job's container;
on HPC, :action:`build-on-hpc` submits it as a SLURM job (see :doc:`hpc`).
Either way the recipe renders to one shell script,
and ``ci-infrastructure-render`` prints that same script for you to run by hand.

Shared Jinja templates
----------------------

``ci-infrastructure`` ships Jinja templates that a recipe extends.
So far the following templates exist:

.. list-table::
   :header-rows: 1

   * - template
     - for
   * - `ci-infrastructure/cmake-runner.sh.j2 <https://github.com/ecmwf/ci-infrastructure/blob/main/src/ci_infrastructure/templates/cmake-runner.sh.j2>`__
     - a GitHub runner: configure, build, test and install one CMake project
   * - `ci-infrastructure/cmake-atos.sh.j2 <https://github.com/ecmwf/ci-infrastructure/blob/main/src/ci_infrastructure/templates/cmake-atos.sh.j2>`__
     - HPC: extends the runner template and adds the ``#SBATCH`` header, the modules,
       the node-local build tree and the install archive
   * - `ci-infrastructure/cmake-all-lanes.sh.j2 <https://github.com/ecmwf/ci-infrastructure/blob/main/src/ci_infrastructure/templates/cmake-all-lanes.sh.j2>`__
     - every lane: extends one of the two above, chosen by the leg's ``execution``
   * - `ci-infrastructure/python.j2 <https://github.com/ecmwf/ci-infrastructure/blob/main/src/ci_infrastructure/templates/python.j2>`__
     - imported, not extended: ``get_python_via_uv`` (see `A Python for the build`_)
   * - `ci-infrastructure/python-runner.sh.j2 <https://github.com/ecmwf/ci-infrastructure/blob/main/src/ci_infrastructure/templates/python-runner.sh.j2>`__,
       `python-atos.sh.j2 <https://github.com/ecmwf/ci-infrastructure/blob/main/src/ci_infrastructure/templates/python-atos.sh.j2>`__,
       `python-all-lanes.sh.j2 <https://github.com/ecmwf/ci-infrastructure/blob/main/src/ci_infrastructure/templates/python-all-lanes.sh.j2>`__
     - a pure Python project: build a wheel, install it with the deps' wheels, run pytest
       (see `A pure Python project`_)

:doc:`../reference/templates` lists every template with its blocks; each name links to its source.

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

Point the legs of every kind at it; the recipe's ``test`` block runs the tests:

.. code:: toml

   [matrix.build.defaults]
   job-script = "./.ci/build.sh.j2"

   [matrix.build-hpc.defaults]
   job-script = "./.ci/build.sh.j2"

A recipe may also extend one lane's template directly, ``cmake-runner.sh.j2`` or ``cmake-atos.sh.j2``,
and serve only that lane.
A package with a recipe per lane keeps them side by side,
``.ci/runner/build.sh.j2`` and ``.ci/hpc/build.sh.j2``;
a recipe for all lanes is ``.ci/build.sh.j2``.

A leg names its compilers in ``c-compiler``, ``cxx-compiler`` and ``fortran-compiler``.
They identify the build, and by default they are also the binaries the recipe calls.
Where a binary is named differently, e.g. ``g++`` from a module for the ``g++-8`` build,
the leg sets ``c-compiler-binary``, ``cxx-compiler-binary`` or ``fortran-compiler-binary``.
Name the C compiler too: left out, CMake picks the image's default ``cc``.

A Python for the build
----------------------

A leg sets ``python-version`` as a string, e.g. ``"3.11"``, never a number.
``get_python_via_uv`` from ``ci-infrastructure/python.j2`` creates a venv with that Python and activates it;
uv downloads the Python if the image lacks it.
CMake's FindPython then finds the venv first.

.. code:: jinja

   {% extends "ci-infrastructure/cmake-runner.sh.j2" %}
   {% import "ci-infrastructure/python.j2" as py %}
   {% block set_environment %}
   {{ py.get_python_via_uv(python_version) }}
   uv pip install -r python/requirements-build.txt
   {% endblock %}

On the HPC, load a ``python3`` module in the leg's ``modules`` and create the venv in the recipe yourself.

A pure Python project
---------------------

A package that compiles nothing extends ``python-runner.sh.j2``, ``python-atos.sh.j2``
or, for both lanes, ``python-all-lanes.sh.j2``.
The recipe builds a wheel of ``$CI_SOURCE_DIR`` with ``uv build`` into ``$CI_INSTALL_PREFIX/wheels``,
installs it into a fresh venv together with the wheels of the leg's deps, runs ``python -m pytest`` in ``$CI_SOURCE_DIR``,
and on HPC archives the install tree.
The wheel is what the leg publishes, and what a downstream Python package installs:
the deps' wheels are taken from ``<prefix>/wheels`` of each prefix on ``CMAKE_PREFIX_PATH``,
in one resolve with the package, so a dep's wheel wins over the same package from the index.
The tests run against the installed wheel, i.e. what users get.

A dep is requested without a Python version, so a pure Python wheel is published by a kind whose leg has no ``python-version``,
here ``wheel``, and the kinds that test it on several Pythons set ``publishes = false``.
The wheel's ``Requires-Python`` still keeps it out of a venv whose Python the package does not support.

A wheel with compiled extensions is built per Python, e.g. ``cp312``, so its producer publishes one leg per ``python-version``,
and the consumer requests the one matching its leg with ``python-version-input``, which names the leg field to read it from:

.. code:: toml

   [[deps]]
   repo = "ecmwf/myextension"
   package = "myextension"
   ref = "main"
   compiler-inputs = ["cxx-compiler"]
   python-version-input = "python-version"

A consumer leg with ``python-version = "3.12"`` then takes the producer's 3.12 leg;
without such a leg the resolve fails.
A pure Python producer can publish from its test legs in the same way:
a consumer leg then only runs against an upstream that passed its tests on the same Python,
and a new Python is added upstream first.

.. code:: toml

   [package]
   name = "mypkg"
   repo = "ecmwf/mypkg"
   compiler-inputs = []

   [[deps]]
   repo = "ecmwf/myupstream"
   package = "myupstream"
   ref = "main"
   compiler-inputs = []

   [matrix.wheel]
   triggers = ["upstream-change", "rebuild-request"]

   [[matrix.wheel.include]]
   job-script = "./.ci/test.sh.j2"
   runs-on = "arc-runner-normal"
   container = "eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest"
   platform = "ubuntu-24.04"
   tests = false

   [matrix.test]
   triggers = ["upstream-change", "rebuild-request"]
   publishes = false

   [matrix.test.defaults]
   job-script = "./.ci/test.sh.j2"
   runs-on = "arc-runner-normal"
   container = "eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest"
   platform = "ubuntu-24.04"
   extras = "tests"

   [[matrix.test.include]]
   python-version = "3.11"

   [[matrix.test.include]]
   python-version = "3.13"

   [matrix.test-hpc]
   execution = "hpc-atos"
   publishes = false

   [[matrix.test-hpc.include]]
   job-script = "./.ci/test.sh.j2"
   runs-on = "hpc-submit"
   container = "eccr.ecmwf.int/private-ci-images/ubuntu24.04-internal-tools"
   site = "hpc-batch"
   platform = "hpc-atos"
   modules = ["load uv"]
   python-version = "3.12"
   extras = "tests,gpu"
   pytest-args = "-m gpu"
   qos = "ng"
   gpus-per-task = 1

.. code:: jinja

   {% extends "ci-infrastructure/python-all-lanes.sh.j2" %}

A leg may set these fields:

.. list-table::
   :header-rows: 1

   * - field
     - default
   * - ``python-version``
     - ``""``: the venv takes the Python uv finds, e.g. a ``python3`` module on HPC;
       otherwise a uv-managed CPython of that version (see `A Python for the build`_)
   * - ``extras``
     - ``""``; e.g. ``"all,tests"`` installs ``<wheel>[all,tests]``
   * - ``pytest-args``
     - ``""``; inserted unquoted, e.g. ``"-n auto -m 'not slow'"``
   * - ``tests``
     - ``true``; ``false`` skips installing and running pytest
   * - ``qos``, ``nodes``, ``ntasks``, ``cpus-per-task``, ``gpus-per-task``, ``mem``, ``ssdtmp``, ``time``
     - HPC only: as for ``cmake-atos.sh.j2`` (see :doc:`../reference/hpc`),
       except ``ntasks`` ``1``, ``mem`` ``16GB`` and no ``ssdtmp``, as nothing is compiled

The recipe overrides blocks as usual:
``set_environment`` runs before the venv, e.g. ``CI_SOURCE_DIR="$CI_SOURCE_DIR/python"`` for a package in a subdirectory;
``test_requirements`` (default ``pytest``) names what else to install for the tests, e.g. ``pytest pytest-xdist``;
``test`` replaces the pytest call.
On HPC, uv must be on ``PATH``, e.g. through ``load uv`` in ``modules``.

Rust bindings
-------------

Rust bindings are checked in a kind of their own, against the artifact of the library they bind,
so a failing ``cargo`` step fails only that kind and the library is still published.
The kind builds a package that publishes nothing and depends on the library:

.. code:: toml

   [packages.eckit-rust]
   compiler-inputs = ["cxx-compiler", "rust-compiler"]

   [[packages.eckit-rust.deps]]
   package = "eckit"
   options-input = "options"

   [matrix.rust]
   packages = ["eckit-rust"]
   publishes = false

   [matrix.rust.defaults]
   job-script = "./.ci/rust.sh.j2"
   cxx-compiler = "g++-13"
   c-compiler = "gcc-13"
   container = "eccr.ecmwf.int/public-ci-images/ubuntu24.04-gcc13-gfortran13-boost-qt6-rust1.90"
   platform = "ubuntu-24.04"

   [[matrix.rust.include]]
   rust-compiler = "rust-1.90"

``rust-compiler`` names the toolchain as ``cxx-compiler`` names g++; the image provides it
(see :doc:`../reference/runners`). Both are ``compiler-inputs``, as both toolchains build what the leg checks.
The recipe extends ``rust-bindings-runner.sh.j2``, which runs ``cargo fmt``, ``clippy``, ``test`` and ``doc`` in ``rust/``
with the ``-sys`` crates in ``system`` mode, linking the library on ``CMAKE_PREFIX_PATH``.
Override ``cargo_features`` for other features, ``cargo_doc`` to skip the docs,
``cargo_workspace`` for another directory:

.. code:: jinja

   {% extends "ci-infrastructure/rust-bindings-runner.sh.j2" %}
   {% block cargo_features %}--no-default-features --features system,raw{% endblock %}
   {% block cargo_doc %}{% endblock %}

A ``-sys`` crate that builds its library from source itself needs only the library's deps,
which the dependency on the library brings along.

Crates of an upstream repo
~~~~~~~~~~~~~~~~~~~~~~~~~~

``Cargo.toml`` names an upstream's crates by their registry version, e.g. ``eckit-sys = { version = "2.5" }``,
and the manifest lists them in ``crates`` on the dep that brings the upstream's library:

.. code:: toml

   [[packages.eccodes-rust.deps]]
   repo = "ecmwf/eckit"
   package = "eckit"
   ref = "develop"
   crates = ["eckit-sys"]

The leg patches each listed crate, through ``cargo --config patch...``, to the commit :action:`resolve-deps` built the dep's artifact from,
whatever the manifest's ``ref`` resolved to, so the Rust code matches the C++ it links against.
It fails where ``Cargo.toml`` and the manifest disagree: a listed crate ``Cargo.toml`` does not depend on,
a crate ``Cargo.toml`` takes over git from a dep's repo without the manifest listing it,
or a version requirement the crate at that commit does not meet.

Publish the crates
~~~~~~~~~~~~~~~~~~

Publishing is a kind of its own, which only a publish workflow resolves, never ``ci.yml``.
Its recipe extends ``rust-bindings-publish.sh.j2``, which runs ``cargo publish --workspace`` in ``rust/``.
It patches no dependency and builds the crates' default features, so it checks what a registry user gets:
the crates' dependencies from the registry, and each packaged ``-sys`` crate building its C++ from the tag of its own version.
That tag must exist, so a dry run of an unreleased version fails.
The kind's package has no deps, and the kind has one leg, as a second would publish the crates again:

.. code:: toml

   [packages.eckit-crates]
   compiler-inputs = ["cxx-compiler", "rust-compiler"]

   [matrix.rust-publish]
   packages = ["eckit-crates"]
   publishes = false

   [[matrix.rust-publish.include]]
   job-script = "./.ci/rust-publish.sh.j2"
   cxx-compiler = "g++-13"
   c-compiler = "gcc-13"
   rust-compiler = "rust-1.90"
   container = "eccr.ecmwf.int/public-ci-images/ubuntu24.04-gcc13-gfortran13-boost-qt6-rust1.90"
   platform = "ubuntu-24.04"

``CI_CARGO_PUBLISH=publish`` publishes; unset or ``dry-run`` stops short of the upload.
Cargo takes the registry token from ``CARGO_REGISTRY_TOKEN``;
a registry other than crates.io is named in ``CARGO_REGISTRY_DEFAULT``,
with its token in ``CARGO_REGISTRIES_<NAME>_TOKEN`` and its index in the workspace's ``.cargo/config.toml``.
Cargo publishes the crates in dependency order and leaves out those with ``publish = false``.

A registry never takes a version twice.
A crate that moves on its own schedule, so that a release finds its version on crates.io already,
is named in the leg's ``cargo-skip-published``, and the publish leaves it out:

.. code:: toml

   [[matrix.rust-publish.include]]
   cargo-skip-published = ["foo-compat"]

Any other crate the registry has at its version fails the publish, as a forgotten version bump should.

A ``publish-crates.yml``, condensed. Pushing a release tag publishes;
a manual run, from the Actions UI or ``gh workflow run publish-crates.yml -f mode=dry-run``, does what ``mode`` says,
but publishes only from a tag:

.. code:: yaml

   name: publish-crates

   on:
     push:
       tags: ['[0-9]+.[0-9]+.[0-9]+']
     workflow_dispatch:
       inputs:
         mode:
           type: choice
           options: [dry-run, publish]
           default: dry-run

   permissions:
     contents: read

   jobs:
     resolve:
       runs-on: arc-runner-normal
       container:
         image: eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest
       outputs:
         matrix: ${{ toJSON(fromJSON(steps.r.outputs.json)['rust-publish']) }}
       steps:
         - uses: actions/checkout@v6
         - id: r
           uses: ecmwf/ci-infrastructure/actions/resolve-deps@main
           with:
             matrix: rust-publish

     publish:
       needs: resolve
       name: publish (${{ matrix._resolved['job-name'] }})
       runs-on: ${{ matrix['runs-on'] }}
       container:
         image: ${{ matrix.container }}
       strategy:
         matrix: ${{ fromJSON(needs.resolve.outputs.matrix) }}
       env:
         CI_CARGO_PUBLISH: ${{ github.ref_type == 'tag' && (inputs.mode || 'publish') || 'dry-run' }}
         CARGO_REGISTRY_TOKEN: ${{ github.ref_type == 'tag' && secrets.CARGO_REGISTRY_TOKEN || '' }}
       steps:
         - uses: ecmwf/ci-infrastructure/actions/announce-image@main
         - uses: actions/checkout@v6
         - uses: ecmwf/ci-infrastructure/actions/run-job-script@main
           with:
             job-script: ${{ matrix.job-script }}
             matrix-leg: ${{ toJSON(matrix) }}
             package: eckit-crates

The tag's code runs with the registry token, as a pull request's never does.
Run the leg by hand the same way, with ``ci-infrastructure-render`` and ``CI_CARGO_PUBLISH`` in the shell.

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
   * - ``CI_INSTALL_ROOT``
     - ``$PWD/_ci/install``, holding ``<name>`` per package of a kind that publishes several
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
