.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Releases (CD v3, experimental)
==============================

.. warning::

   Experimental, on the ``feature/cd-v3`` branch only. It publishes **prereleases to test
   Nexus** and rpms only; HPC modules, deb, conda and PyPI come later. v2
   (``ecmwf/reusable-workflows``) stays the production publisher.

A release is built from the commit CI passed on, by the same job-script recipe CI runs, into
a package that is install-tested in a clean image and then published, at most once, by a job
that alone holds the publish credential.

The pieces
----------

``.github/workflows/cd-v3.yml``
   Copied from ``samples/cd/cd.yml``: ``ci-approval`` → ``gate`` → ``build-rpm`` → ``itest-rpm`` →
   ``publish-nexus`` (the publish job) → ``record``.
``[release]`` and ``release-ref`` in the manifest
   What the package is called and what the release builds against.
``[matrix.release-<channel>]``
   The release legs: a kind named ``release-…``, ``publishes = false``, no ``triggers``, its
   ``packages`` listing what it builds.
The release overlay
   ``templates/release/``: for a leg the gate gives a ``_release`` context, a recipe that
   extends ``ci-infrastructure/cmake-all-lanes.sh.j2`` gets the release build instead of the
   CI one. A recipe must extend that template: one that extends ``cmake-runner.sh.j2`` or
   ``cmake-atos.sh.j2`` directly is refused for a release leg, as it would build as CI does.

The manifest
------------

.. code-block:: toml

   [release]
   package-name = "eckit-cd-test"     # default: [package].name
   tag-prefix = ""                    # "" or "v"
   version-parts = 3                  # 3 or 4
   summary = "ECMWF C++ toolkit (CD v3 test copy)"
   license = "Apache-2.0"
   vendor = "ECMWF"
   maintainer = "software@ecmwf.int"
   homepage = "https://github.com/ecmwf/eckit"

   [[deps]]
   repo = "ecmwf/ecbuild"
   package = "ecbuild"
   ref = "develop"                    # CI
   compiler-inputs = []
   release-ref = "3.16.0"             # releases: this tag, fetched as source

   [[deps]]
   repo = "ecmwf/stack-dependencies"
   package = ["libaec", "lz4"]
   ref = "master"
   compiler-inputs = ["cxx-compiler"]
   release-ref = false                # releases: from the leg's image

   [packages.eckit-cd-test-release]   # what the release kind builds: no deps of its own
   compiler-inputs = ["cxx-compiler"]

   [matrix.release-rpm]
   packages = ["eckit-cd-test-release"]
   publishes = false

   [[matrix.release-rpm.include]]
   job-script = "./.ci/build.sh.j2"
   channel = "rpm"
   platform = "rocky-9"
   cxx-compiler = "g++-11"
   c-compiler = "gcc-11"
   container = "eccr.ecmwf.int/public-ci-images/rocky9-gcc11-gfortran11:latest"
   runs-on = "arc-runner-very-large"
   build-type = "Release"
   options = "release"                # the CMake preset, as in CI
   install-prefix = "/opt/ecmwf"
   arch = "x86_64"
   nexus-target = "rocky-9"
   install-test-image = "eccr.ecmwf.int/docker-hub-proxy/rockylinux/rockylinux:9"
   install-test-command = "/opt/ecmwf/bin/eckit-version"

The dependency rule:

- With ``[release]`` present, every top-level ``[[deps]]`` with a ``repo`` sets ``release-ref``.
- A release leg gets **exactly** the top-level ``[[deps]]`` whose ``release-ref`` is a string:
  a tag or a 40-character SHA, never a branch, and no ``sync/`` or ``feature/`` override. The gate
  resolves it to a commit. For now these must be source deps (``compiler-inputs = []``);
  ``actions/release-source-deps`` checks each out at its commit and puts it on ``CMAKE_PREFIX_PATH``.
- ``release-ref = false``: the dep comes from the leg's image or modules.
- A package a release kind builds declares no deps of its own, so the ``release-ref`` sits next to
  the CI ``ref`` of the same dep, where the release owner bumps it.

A release leg must set ``channel``, ``options``, ``build-type``, ``install-prefix``, ``arch``,
``nexus-target``, ``install-test-image`` and ``install-test-command``; its ``container`` and
``install-test-image`` come from ``eccr.ecmwf.int/``; it must not set ``tests = false``. Images are
``:latest``: ``build-meta.json`` records ``CI_IMAGE_NAME`` and ``CI_IMAGE_TAG`` of each build, as
the registry keeps only the two newest versions of a pinned tag.

Versions
--------

A release tag is ``<tag-prefix>X.Y.Z`` (or ``X.Y.Z.W``), optionally ``-rcN``, case-insensitive
(``-RC1`` too). Only ``-rcN`` is a prerelease; ``-alpha`` or ``-beta`` is not a release tag. The
tag's ``X.Y.Z`` must be the ``VERSION`` file (whitespace stripped), or the CMake
``project(... VERSION ...)`` when there is none.

.. list-table::
   :header-rows: 1

   * - mode
     - starts from
     - rpm Version-Release
     - published
   * - rehearsal
     - a same-repo pull request labelled ``approved-for-cd``
     - ``2.5.0-1``
     - no
   * - prerelease
     - tag ``2.5.0-rc1``
     - ``2.5.0~rc1-1``
     - test Nexus
   * - release
     - tag ``2.5.0``
     - ``2.5.0-1``
     - refused for now

The rc rpm is ``X.Y.Z~rcN-1``, which sorts before ``X.Y.Z-1``: a deliberate change from v2, which
published an rc under the plain VERSION (``2.5.0-1``), so the rc and the release collided.

The gate
--------

``actions/release-gate`` decides whether a run may release, and what. In order:

1. ``CD_KILL_SWITCH=on`` (a repo or org variable) stops every release, with ``CD_KILL_SWITCH_REASON``.
2. The event: a release tag push, or a same-repo pull request labelled ``approved-for-cd``.
3. ``release`` mode (no ``-rcN``) is refused without allow-production.
4. The tag is the project's VERSION.
5. CI passed on exactly this commit: a ``ci.yml`` run of this repo (never a fork's), event
   ``push`` or ``workflow_dispatch``, on the commit. A rehearsal also accepts the pull request's
   own ``pull_request`` run on its head commit. It builds ``github.sha``, the merge commit, which
   is the commit PR CI tested only as long as the base has not moved since; the head SHA is
   what is matched and recorded. While CI is running, or has not started, the gate waits, polling
   every minute up to ``ci-wait-seconds`` (``ARTIFACT_WAIT_TIMEOUT``, else two hours). A failed run
   refuses at once. A ``ci-result`` status is never trusted: any workflow can post one.
6. The tag has not moved: an earlier run of this workflow for the tag built the same commit.
7. Each string ``release-ref`` is a tag or commit of its repo; a branch is refused.
8. The workflow file at the commit passes ``check-cd-workflow`` (below).
9. The release legs, each with its ``_release`` context.
10. For a publishing run, each rpm already on Nexus (an anonymous probe of its URL) is marked
    ``already-published``. It stays in the matrix, its build and install test are skipped, and
    the publish job records what Nexus holds. A probe that gets no answer builds the leg anyway.

Credentials and the threat model
--------------------------------

- Building runs the repo's own code (CMake, tests) and gets no publish credential. Only the
  publish jobs (those that run ``upload-nexus``, ``hpc-promote`` or ``hpc-sync-tag``) hold one,
  and they run no ``run:`` step, check out nothing and build nothing: they move artifacts and
  call ``upload-nexus``.
- No GitHub environment and no repo variable is needed: the workflow uses the org secrets v2
  already uses. The upload credential is ``NEXUS_TEST_REPO_UPLOAD_TOKEN`` (``user:token``), the
  repository URL ``NEXUS_TEST_REPO_URL_ROCKY_9`` (the URL v2 PUTs ``<url>/<file>`` to). An org
  secret is readable by any job that names it, so the check hook allows a non-CI secret only in
  a publish job, and a ``NEXUS_TEST_REPO_URL_*`` URL also in the gate's job, which probes it
  anonymously. Only the tag-push trigger reaches the publish job (``publish`` is false for a
  rehearsal). ``upload-nexus`` takes the credential from the step's ``env``, masks it, and
  refuses any host but test Nexus. GitHub masks the URL in logs too, so it prints as ``***``;
  the receipts and the release record hold it in full.
- ``upload-nexus`` uploads only the bytes the install test passed (their sha256 is in its
  receipt, with the commit and job key of the build it tested, which must be the leg's), and
  never overwrites: the same bytes are a no-op, other bytes fail.
- The test Nexus repository is expected to have **redeploy disabled** (deployment policy
  "Disable redeploy"): that server-side setting is what makes a published file immutable.
  ``upload-nexus``'s GET-then-PUT check only complements it; two uploads racing between the GET
  and the PUT are caught by the server, not the client.
- The release build refuses ``SCCACHE_BUCKET`` and compiler launchers, and the workflow names no
  CI cache or artifact-store secret: a release is built from source alone.
- **Known gap: ci-infrastructure cannot be pinned by SHA yet.** Every action nests
  ``ensure-infrastructure-present`` at a fixed ref (``@feature/cd-v3`` on this branch, ``@main``
  on main), which installs that ref's Python whatever ref the consumer named. So the
  publish jobs run ci-infrastructure code at a mutable branch ref during the experiment.
  A fix is for the bootstrap to install from the outer action's checkout (a ``source-path``
  input); until then ``check-cd-workflow`` accepts ``--allow-ref feature/cd-v3``.
- **Known gap: images are** ``:latest`` (see above).

The workflow's shape
--------------------

``check-cd-workflow`` (a pre-commit hook, and gate check 8) holds ``cd-v3.yml`` to the shape
of ``samples/cd/cd.yml``: tag and labelled-PR triggers only; ``contents: read``; a concurrency
group on the ref, never cancelled; no ``secrets: inherit`` or workflow ``env``; a non-CI secret
only in a publish job (a ``NEXUS_TEST_REPO_URL_*`` also in the gate's job); publish jobs as
above, on an ECMWF runner, in an ECCR container, with no ``env`` that changes how processes start (``LD_*``, ``BASH_ENV``, ``PATH``, ``PYTHON*``,
``GIT_*``, ...), downloading only this run's artifacts (no ``run-id``, ``repository`` or
``github-token``), needing every build and test job and running exactly
``if: ${{ !cancelled() && needs.gate.outputs.publish == 'true' && !contains(needs.*.result, 'failure') }}``,
with a ``name`` that uses no ``matrix`` (GitHub shows it unevaluated for a skipped job);
no ``${{ }}`` inside ``run:``; one ci-infrastructure ref, other actions pinned by SHA; every job
needing ``ci-approval`` and ``gate`` directly; every ``upload-artifact`` with ``overwrite: true``.

.. code-block:: yaml

   - repo: https://github.com/ecmwf/ci-infrastructure
     rev: <feature/cd-v3 SHA>
     hooks:
       - id: check-cd-workflow
         args: [--allow-ref, feature/cd-v3]

Re-runs and resume
------------------

.. list-table::
   :header-rows: 1

   * - situation
     - rpm (Nexus)
   * - Re-run failed jobs (same artifacts)
     - the same bytes are on Nexus: no-op
   * - Re-run all, or a new run for the same tag and commit
     - the gate's probe finds it: build skipped, publish records it
   * - Same version, different commit
     - the gate refuses a moved tag; an upload of other bytes fails
   * - More than 30 days later (artifacts expired)
     - a new rc

Every upload sets ``overwrite: true``, so a re-run job can replace its own artifact; the
artifact names carry the ``job_key`` (package, tag, leg, commit), so runs never share one.
``record`` writes ``release-record.json`` (the gate's provenance, and per leg its build
metadata, install test and Nexus receipt) and fails as *partial* when a piece is missing:
"Re-run failed jobs" completes the release.

Locally
-------

.. code-block:: console

   $ ci-infrastructure-render --list
   $ ci-infrastructure-render --leg 'release-rpm (rocky-9, g++-11, release)' \
       --release prerelease --tag 2.5.0-rc1 -o release.sh

renders the build script exactly as the gate hands it to ``run-job-script``: the CPack cache
and build metadata are rendered into it, so it runs with nothing but bash and the leg's image.
