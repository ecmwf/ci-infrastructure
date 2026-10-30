.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Releases (CD v3, experimental)
==============================

.. warning::

   Experimental, on the ``feature/cd-v3`` branch only. It publishes **prereleases**: rpms to
   **test Nexus**, and HPC modules into a **dry-run root** (``$SCRATCH/cd-v3-dry-run``, never
   ``/usr/local/apps``) whose module sync and tag are only printed. deb, conda and PyPI come
   later. v2 (``ecmwf/reusable-workflows``) stays the production publisher.

A release is built from the commit CI passed on, by the same job-script recipe CI runs, into
a package that is install-tested in a clean image and then published, at most once, by a job
that alone holds the publish credential.

The pieces
----------

``.github/workflows/cd-v3.yml``
   Copied from ``samples/cd/cd.yml``: ``ci-approval`` → ``gate`` → ``build-rpm`` → ``itest-rpm``
   and ``build-hpc`` → the publish jobs ``publish-nexus`` and ``promote-hpc`` → ``sync-tag-hpc``
   (a publish job too) → ``record``.
``[release]`` and ``release-ref`` in the manifest
   What the package is called and what the release builds against.
``[matrix.release-<channel>]``
   The release legs: a kind named ``release-…``, ``publishes = false``, no ``triggers``, its
   ``packages`` listing what it builds.
The release overlay
   ``templates/release/``: for a leg the gate gives a ``_release`` context, a recipe that
   extends ``ci-infrastructure/cmake-all-lanes.sh.j2`` gets the release build instead of the
   CI one: ``cmake-runner-release.sh.j2`` (an rpm, on a runner) or ``cmake-atos-release.sh.j2``
   (an HPC module, in a SLURM job), each extending the CI template of its lane, which stays
   unchanged. A recipe must extend that template: one that extends ``cmake-runner.sh.j2`` or
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
   # Installed files left out of the package, relative to install-prefix (optional).
   exclude = ["lib64/pkgconfig/eckit_test_value_custom_params.pc"]

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

   [matrix.release-hpc]
   packages = ["eckit-cd-test-release"]
   publishes = false
   execution = "hpc-atos"
   container-credentials = true

   [[matrix.release-hpc.include]]
   job-script = "./.ci/build.sh.j2"
   channel = "hpc-module"
   platform = "hpc-atos-gnu"
   cxx-compiler = "g++-8"
   c-compiler-binary = "gcc"
   cxx-compiler-binary = "g++"
   runs-on = "hpc-submit"
   container = "eccr.ecmwf.int/private-ci-images/ubuntu24.04-internal-tools:latest"
   site = "hpc-batch"
   modules = ["load prgenv/gnu", "unload gcc", "load gcc/old", "load cmake", "load ninja"]
   time = "01:00:00"
   build-type = "Release"
   options = "release"
   install-prefix = "{root}/{package}/{version}/GNU/8.5"   # {root}: the gate's hpc-root
   install-test-command = "bin/eckit-version"              # run in the unpacked prefix

The dependency rule:

- With ``[release]`` present, every top-level ``[[deps]]`` with a ``repo`` sets ``release-ref``.
- A release leg gets **exactly** the top-level ``[[deps]]`` whose ``release-ref`` is a string:
  a tag or a 40-character SHA, never a branch, and no ``sync/`` or ``feature/`` override. The gate
  resolves it to a commit. For now these must be source deps (``compiler-inputs = []``);
  ``actions/release-source-deps`` checks each out at its commit and puts it on ``CMAKE_PREFIX_PATH``.
- ``release-ref = false``: the dep comes from the leg's image or modules.
- A package a release kind builds declares no deps of its own, so the ``release-ref`` sits next to
  the CI ``ref`` of the same dep, where the release owner bumps it.

A release leg must set ``channel``, ``options``, ``build-type``, ``install-prefix``,
``install-test-command`` and a ``container`` from ``eccr.ecmwf.int/``, and must not set
``tests = false``. Per channel:

- ``rpm`` (``execution = "runner"``): ``arch``, ``nexus-target``, an ``install-test-image`` from
  ``eccr.ecmwf.int/``; ``install-prefix`` is absolute (``/usr/local/apps`` is refused).
- ``hpc-module`` (``execution = "hpc-atos"``): ``site`` (a troika site); ``modules``;
  ``install-prefix`` starts with ``{root}/{package}/{version}`` (then plain components, e.g.
  ``/GNU/8.5``), where ``{root}`` is the gate's ``hpc-root`` (default ``$SCRATCH/cd-v3-dry-run``,
  expanded on the cluster), ``{package}`` the package name and ``{version}`` the tag as pushed
  (``2.5.0-rc1``; ``2.5.0-RC1`` stays as is, as in v2) or, in a rehearsal, the VERSION;
  ``install-test-command`` is relative to the prefix (``bin/...``).

Images are ``:latest``: ``build-meta.json`` records ``CI_IMAGE_NAME`` and ``CI_IMAGE_TAG`` of each
rpm build, as the registry keeps only the two newest versions of a pinned tag.

``[release] exclude`` lists globs, relative to the leg's ``install-prefix``, of installed files to
leave out of the package: for a file the project installs by mistake, such as a test-only
``.pc`` that points into the build tree. The patterns are rendered into the build script as a
CMake script that CPack runs on its staging tree before it packages
(``cpack -D CPACK_PRE_BUILD_SCRIPTS=...``, CMake 3.19 or newer; the script fails if CPack did not
run it). The rpm, and so the install test, never hold the file; the build log names each removed
path. The syntax is CMake ``file(GLOB)``: ``*``, ``?`` and ``[...]``, each within one path
component (``**`` is refused). A pattern is relative, with no ``.`` or ``..`` component, and uses
only ``A-Z a-z 0-9 . _ + @ = , ~ - /`` besides. All patterns are expanded before anything is
removed, and one that matches nothing fails the build, so a stale exclusion cannot linger: drop it
once the project stops installing the file. An exclusion hides a file from the install tree's
checks too, so it is for files that should not ship, never to silence a leak in one that should.
An HPC module has no CPack: there the same globs are removed from the installed tree (bash
globs, also each matching something) before it is archived.

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

The HPC module of a prerelease is ``<root>/<package>/2.5.0-rc1/...``, synced but never tagged;
a release would be ``<root>/<package>/2.5.0/...``, tagged ``new``. A rehearsal installs nothing.

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
10. For a publishing run, each rpm already on Nexus (a ``HEAD`` of its URL) is marked
    ``already-published``. It stays in the matrix, its build and install test are skipped, and
    the publish job records what Nexus holds. The probe authenticates with the Nexus credential
    from the release-gate step's ``env: NEXUS_AUTH`` (``NEXUS_TEST_REPO_UPLOAD_TOKEN``): the test
    repositories are private, and an anonymous ``HEAD`` of a published rpm gets ``403`` (seen on
    ``private-rocky-stable`` after the ``2.5.0-rc2`` release), so an anonymous probe never finds
    one; "Re-run all jobs" then rebuilds the leg and ``upload-nexus`` refuses the new bytes.
    ``200`` is published, ``404`` is not; ``401``/``403`` to the authenticated probe refuses the
    release (it cannot tell). A probe that gets no answer (unreachable, ``5xx`` after retries)
    builds the leg anyway. Without ``NEXUS_AUTH`` the probe is anonymous and the gate warns.
    An HPC leg is not probed by the gate, which cannot reach the cluster: its build job looks at
    the leg's prefix first (below).

HPC modules
-----------

``build-hpc`` runs ``build-on-hpc`` with ``publish: archive`` (``check-cd-workflow`` refuses any
other mode in a release: it would use the CI artifact store):

1. In a publishing run it first expands the leg's prefix on the cluster and reads
   ``<prefix>/.release.json``. This release there already: nothing is built, and the artifact holds
   ``state.json`` only (``already-published``). Another release, or a tree without
   ``.release.json``: refused. A rehearsal never looks.
2. The SLURM job is named ``ci-<job_key>`` (package, tag, leg, commit): a run of ``rc2`` never
   reattaches to an ``rc1`` job, nor shares its install dir, which the job empties first. The job
   script gets the prefix as expanded on the cluster, and needs nothing but bash and the leg's
   modules: everything release-specific is rendered into it.
3. The job configures for the final prefix (no RPATH settings of its own: ecbuild's relative
   RPATHs, as v2), builds, runs the tests, installs into a staging tree (``DESTDIR``), removes
   ``[release] exclude``, and archives the prefix's tree (``.tar.zst``, relative to the prefix).
4. **The install test, in the same job:** the build and staging trees are deleted, modules purged
   and reloaded, ``LD_LIBRARY_PATH`` unset; the archive's digest is checked and it is unpacked
   fresh; every ELF file's RUNPATH may name only ``$ORIGIN``, the prefix and the modules' library
   directories; ``ldd`` must find every library; no build path (``$TMPDIR``, the source and
   staging trees, ``github-ci/``) may be in a CMake, pkg-config or ``share/`` file; and the smoke
   command runs in the unpacked prefix. A tree that is not relocatable fails here.
5. ``build-on-hpc`` fetches the archive and its sidecars, never unpacked, and refuses them unless
   the install test passed exactly that digest, for that commit and job key. They are uploaded as
   ``release-<job_key>``: the archive, ``SHA256SUMS``, ``build-meta.json``,
   ``itest-<leg_id>.json``, ``modules.txt`` and the job's ``build.log``.

``promote-hpc`` (``hpc-promote``) then, per leg: checks the archive is the tested bytes and safe to
unpack (no absolute or ``..`` names, no link out of the tree, also through the archive's own
symlinks, no device, no setuid, no ``.release.json`` or ``build.log`` at its root); takes the lock
``<lock-root>/promote/<package>``; and, when the prefix holds this release with the same digest,
does nothing; with other bytes or another release, fails (an installed tree is never replaced:
tag a new rc). Otherwise it removes what an interrupted promote left (``.<leaf>.incoming.*``),
sends the archive and checks its digest there, unpacks it into a new ``.incoming`` sibling, adds
``.release.json`` and ``build.log`` only where nothing (not even a symlink) is, makes it read-only and renames it onto the prefix
(``mv -T -n``: atomic, never onto an existing tree). Its receipt joins the record.

``sync-tag-hpc`` (``hpc-sync-tag``) takes the one cross-repo lock ``<lock-root>/modulemgr``,
checks each promoted prefix, and prints the ``modulemgr`` / ``software-sync`` commands v2 would
run (sync to the other clusters; a release, never a prerelease, tagged ``new``). In milestone 1 it
runs none of them: it never calls ``software-sync`` or ``modulemgr``.

Where a module is synced to and tagged on comes from ``module-clusters.yml``, keyed by the
*cluster site*: the leg's troika ``site``, or the ``cluster-site`` input of ``hpc-sync-tag`` (for a
module built through another site name). Unlike v2, nothing is guessed, and nothing is ever
synced from or to, or tagged on, ``ag``:

- A cluster site the map does not list is refused, before the cluster is touched. ``hpc-batch`` is
  not listed: it lands on any cluster, so v2's fallback to ``aa-batch`` would sync the wrong
  cluster's tree (``2.5.0-rc5`` was built on ``ac`` and planned ``-m ab,ac,ad``). Build and promote
  on ``aa-batch`` … ``ad-batch``, or name the one with ``cluster-site``.
- A cluster site on ``ag``, or one whose sync or tag targets name ``ag``, is refused. The map keeps
  v2's ``ag-batch`` entries (no sync, tag on ``ag``) for parity, but ``ag-batch`` is always refused,
  so ``ag`` is never tagged either.
- Under the lock it reads the cluster the connection runs on from its ``hostname`` (an Atos node
  such as ``ac6-115.bullx`` is ``ac``; ``$EC_CLUSTER``, when the login shell sets it, must agree),
  and refuses when that is ``ag``, cannot be told, or is not the cluster site's cluster.

.. note::

   Open item: the cluster map (which sites there are, the cluster each is on, and the hostname
   rule) is ported from v2 and must be confirmed with the HPC admins before a real sync is
   enabled.

Locks and the kill switch
~~~~~~~~~~~~~~~~~~~~~~~~~

The lock root is ``$HOME/.cd-locks`` of the troika user (``lock-root``). A lock is a directory made
with ``mkdir`` and an ``owner`` file (one line of JSON: run, repository, job, host, pid, time). A
holder refreshes it every minute (heartbeat), so a lock unchanged for ``lock-stale-minutes``
(default 120) is a dead holder's: one waiter at a time (sub-lock ``<lock>.breaking``) looks at it
again, renames it aside, removes it only when it is still the stale lock it looked at (same owner,
still old; else it is renamed back with ``mv -T -n``, or left aside with a warning, never
deleted), and prints whose it was. A waiter gives up after ``lock-timeout`` seconds (default 1800), naming the holder. A
holder removes the lock only while its owner file is still its own. ``<lock-root>/DISABLED``
(its content is the reason) stops every promote and sync/tag on that HPC, checked before and
again after taking the lock; a check that cannot run (ssh failure, filesystem error) stops them too; ``CD_KILL_SWITCH`` stops the gate.

To see or clear a lock by hand: ``cat $HOME/.cd-locks/promote/<package>/owner``; remove the
directory only when that run is gone.

The lock is a lease without fencing tokens: a holder that is alive but cannot touch its lock for
``lock-stale-minutes`` and then resumes can overlap with the waiter that broke it. That needs a
two-hour stall against a one-minute heartbeat; the last line of defence is promote's
``mv -T -n`` onto the prefix and its read-back, so two promotes of one prefix cannot both succeed.

Credentials and the threat model
--------------------------------

- Building runs the repo's own code (CMake, tests) and gets no publish credential. Only the
  publish jobs (those that run ``upload-nexus``, ``hpc-promote`` or ``hpc-sync-tag``) hold one,
  and they run no ``run:`` step, check out nothing and build nothing: they move artifacts and
  call ``upload-nexus``. The release gate's job holds the test Nexus credential too, for its
  probe, in the release-gate step's ``env`` only; it is held to the same hardening where it fits:
  no ``run:`` step, only ``actions/checkout`` (``persist-credentials: false``, to read the
  manifest and the workflow) and ``release-gate``, an ECMWF runner, an ECCR container, no
  ``env`` that changes how processes start (in the job, a step or ``container.env``: it reaches
  every process of the job, the release gate's own ``bash`` steps included, before the credential
  is masked), and no ``container.options``, ``container.volumes`` or ``services`` (``--env``, ``--volume``,
  ``--entrypoint`` can do the same). The gate masks it, sends it only as the probe's basic
  auth, removes it from its environment before calling ``gh``, and never writes it to an output
  or the provenance.
- No GitHub environment and no repo variable is needed: the workflow uses the org secrets v2
  already uses. The upload credential is ``NEXUS_TEST_REPO_UPLOAD_TOKEN`` (``user:token``), the
  repository URL ``NEXUS_TEST_REPO_URL_ROCKY_9`` (the URL v2 PUTs ``<url>/<file>`` to). An org
  secret is readable by any job that names it, so the check hook allows a non-CI secret only in
  a publish job; in the gate's job also a ``NEXUS_TEST_REPO_URL_*`` URL, and
  ``NEXUS_TEST_REPO_UPLOAD_TOKEN`` only as the release-gate step's ``env: NEXUS_AUTH``. Only the tag-push trigger reaches the publish job (``publish`` is false for a
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
- **HPC identity.** Building, promoting and syncing all run as the CI's HPC user
  (``secrets.HPC_CI_SSH_USER``, allowed in any job), which also runs PR CI: it is not a publish
  credential, which is why milestone 1 only installs into a dry-run root of that user's scratch
  and runs no ``modulemgr``. A production module needs a separate deploy identity (its own ssh
  key on the ``hpc-submit`` runners) that alone can write ``/usr/local/apps``; ``promote-hpc``
  and ``sync-tag-hpc`` are already the only jobs that would hold it.

The workflow's shape
--------------------

``check-cd-workflow`` (a pre-commit hook, and gate check 8) holds ``cd-v3.yml`` to the shape
of ``samples/cd/cd.yml``: tag and labelled-PR triggers only; ``contents: read``; a concurrency
group on the ref, never cancelled; no ``secrets: inherit`` or workflow ``env``; ``build-on-hpc``
only with ``publish: archive``; a non-CI secret
only in a publish job (in the gate's job also a ``NEXUS_TEST_REPO_URL_*``, and the upload token
as the release-gate step's ``NEXUS_AUTH``; that job hardened as above); publish jobs as above, on an ECMWF runner, in an ECCR container, with no ``env`` that changes how processes start (``LD_*``, ``BASH_ENV``, ``PATH``, ``PYTHON*``,
``GIT_*``, ...) in the job, a step or ``container.env``, no ``container.options``,
``container.volumes`` or ``services``, downloading only this run's artifacts (no ``run-id``, ``repository`` or
``github-token``), needing every build and test job and running exactly
``if: ${{ !cancelled() && needs.gate.outputs.publish == 'true' && !contains(needs.*.result, 'failure') }}``,
with a ``name`` that uses no ``matrix``;
no ``${{ }}`` inside ``run:``; one ci-infrastructure ref, other actions pinned by SHA; every job
needing ``ci-approval`` and ``gate`` directly; every ``upload-artifact`` with ``overwrite: true``.

.. code-block:: yaml

   - repo: https://github.com/ecmwf/ci-infrastructure
     rev: <feature/cd-v3 SHA>
     hooks:
       - id: check-cd-workflow
         args: [--allow-ref, feature/cd-v3]

Job names
---------

GitHub does not evaluate the ``name`` of a matrix job it skips: ``publish-nexus`` is skipped in
every rehearsal, and ``name: publish-nexus (${{ matrix._release.leg_id }})`` showed literally
as that text (run ``40902451849`` of eckit-cd-test). Any expression on ``matrix`` is the same,
``format()`` included, so ``check-cd-workflow`` refuses one in a publish job's name. A static
name gets every matrix value of the leg appended
(``publish-nexus (./.ci/build.sh.j2, rpm, rocky-9, g++-11, ...)``): long, but right whether the
job runs or is skipped. ``build-rpm`` and ``itest-rpm`` name the leg (``build-rpm
(${{ matrix._release.leg_id }})``); they are skipped only when there is no rpm leg, and then
show the expression. A short name for both cases would need a matrix of only the leg id, which
is not done.

Re-runs and resume
------------------

.. list-table::
   :header-rows: 1

   * - situation
     - rpm (Nexus)
     - HPC module
   * - Re-run failed jobs (same artifacts)
     - the same bytes are on Nexus: no-op
     - ``.release.json`` has the same digest: no-op
   * - Re-run all, or a new run for the same tag and commit
     - the gate's probe finds it: build skipped, publish records it
     - the build job finds it under the prefix: build skipped, promote records it
   * - Same version, different commit
     - the gate refuses a moved tag; an upload of other bytes fails
     - the gate refuses a moved tag; the build job and promote refuse another release
   * - Interrupted promote
     - n/a
     - the stale lock is broken, ``.incoming`` removed, promote redone
   * - More than 30 days later (artifacts expired)
     - a new rc
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

.. code-block:: console

   $ ci-infrastructure-render --leg 'release-hpc (hpc-atos-gnu, g++-8, release)' \
       --release prerelease --tag 2.5.0-rc1 [--hpc-root '$SCRATCH/cd-v3-dry-run'] -o release-hpc.sh

renders the HPC leg's SLURM job script (``#SBATCH`` header first). ``build-on-hpc`` hands the job
the prefix expanded on the cluster; rendered here it keeps ``"$SCRATCH"``, which bash expands
where it runs.
