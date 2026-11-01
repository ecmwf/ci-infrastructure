.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Trigger downstream repositories
===============================

A change can break the repositories that depend on it, although its own CI is green.
Downstream CI therefore builds and tests these repositories against the change,
and reports the result on the pull request.
It serves two purposes:
it catches a breakage that was not intended,
and it tests an intended breaking change together with the adaptations of its consumers,
which are then prepared on ``sync/`` or ``feature/`` branches of the same name (see :doc:`../using/feature-branches`).

Declaring the graph
-------------------

The producer lists the repositories to build after a change, ``eckit`` for example triggers ``eccodes``:

.. code:: toml

   [[trigger-downstream]]
   repo = "ecmwf/eccodes"
   ref = "develop"

The consumer declares which of its kinds are built on behalf of an outside repo's request:

.. code:: toml

   [matrix.build]
   triggers = ["upstream-change", "rebuild-request"]

this allows to exclude heavy tests, such as ``valgrind``, from the downstream CI.

``triggers = ["upstream-change"]`` builds the kind when an upstream changes,
``"rebuild-request"`` when a consumer finds one of its artifacts missing.
A missing artifact is rebuilt unless that commit's ``CI`` was red:
on the declared ref or sync branch through ``rebuild-request``,
for any other commit, such as one under test, by re-running that commit's ``CI``.
A fork's commit is never rebuilt.
An artifact that is not to be had, such as one of a red commit, fails only the legs that need it,
in their first step, with the errors of the red run and of the runs those link to, up to the first failure.
A request that no producer leg can ever build fails the whole resolve.
The order follows from ``[[deps]]``:
``eccodes``' ``build`` waits for the ``build`` kinds of ``ecbuild``, ``stack-dependencies`` and ``eckit``,
its ``build-hpc`` for their ``build-hpc``.
The downstream graph is a subset of the dependency graph (in the other direction):
every repository in ``[[trigger-downstream]]`` must list the producer in its ``[[deps]]``,
but not every dependency has to trigger its consumers.
The graph is transitive as well, so a change in ``ecbuild`` also reaches ``eccodes`` through ``eckit``.
:doc:`ci-infrastructure-generate <../reference/cli/ci-infrastructure-generate>` checks these rules; see :doc:`../reference/manifest`.

Artifacts built against older upstreams
---------------------------------------

An artifact's name contains the hash of its dependencies' artifact names,
so when ``eckit`` moves, ``eccodes``' artifact for the same ``eccodes`` commit no longer matches,
and every consumer of ``eccodes`` waits for its rebuild, which in turn waits for that of its own upstreams.
A consumer can accept the previous build in its pull requests instead:

.. code:: toml

   [package]
   allow-stale-deps = true   # the default for every [[deps]]

   [[deps]]
   repo = "ecmwf/eccodes"
   package = "eccodes"
   ref = "develop"
   allow-stale = true        # or per dependency

Where the artifact of the dependency is missing or still being built,
the resolver then takes the newest stored build of the same commit and variant
together with the upstreams it was built against, for example ``eckit`` one commit behind.
It does so only if that tree has the same packages, at commits in the history of the current ones,
all still in the store, and if no other dependency, nor a direct one without ``allow-stale``, needs the current builds of them;
otherwise the dependency is rebuilt as before.
The leg's dependency table shows such a dependency with source ``stale artifact``,
and the resolve job notes against which commits it was built.

This applies to the ``pull_request`` and ``pull_request_target`` events only,
never to downstream CI, which tests a commit under test, nor on a sync branch.
After the merge, the build of the default branch resolves the current upstreams again,
so the default branch is always built and tested against them.

The resolver records beside every artifact it resolves which dependencies it was built against
(``<artifact-name>.deps.json`` in the artifact store);
an artifact without this record is never used in place of another.

Nightly builds
--------------

A repository that nobody pushes to keeps its artifacts built against old upstreams,
so its consumers' pull requests fall back to them, or wait for a rebuild.
A ``schedule`` among the triggers of its ``ci.yml`` builds the default branch every night against the current upstreams:

.. code:: yaml

   on:
     # ... the existing triggers ...
     schedule:
       - cron: "37 0 * * *"

Schedule upstreams first, so that a consumer finds their builds already in the store;
the ECMWF stack starts with ``ecbuild`` at 00:07 UTC and takes 15 minutes per level of the graph.
A scheduled run does not fan out to the consumers, which have nightly builds of their own.
The generated ``retry-nightly.yml`` re-runs the failed jobs of a scheduled run up to twice,
unless the branch has moved on since, see :action:`retry-scheduled-run`.
GitHub runs a ``schedule`` only from the default branch,
may start it late or drop it under load,
and disables it in a public repository after 60 days without activity
(`schedule <https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule>`__).

The generated workflows
-----------------------

The workflows that run downstream CI are auto-generated
by :doc:`ci-infrastructure-generate <../reference/cli/ci-infrastructure-generate>`.
Once the workflow ``CI`` of a pull request has completed successfully,
the downstream CI builds every consumer against the commit under test,
in dependency order,
and posts the result as the status ``downstream/runner`` (and ``downstream/hpc-atos``) on the pull request.
The ``hpc-atos`` lane also needs the label ``run-hpc-ci``, without which the commit's ``CI`` built no HPC artifacts;
with ``hpc-ci-not-needed`` instead, its status is a success at once.

The generated files must follow the manifests, also those of the other repositories.
:action:`validate-generated-workflows` reports when they have drifted.
A bot runs :action:`regenerate-workflows-pr` on every push to the default branch and nightly,
and opens a pull request with the regenerated workflows.
It also pins the ``rev`` of the ci-infrastructure hooks in ``.pre-commit-config.yaml`` to the commit that generated them,
so the pull request is checked by the hooks that match its files.
It labels a new pull request ``downstream-ci-not-needed`` and ``hpc-ci-not-needed`` (the input ``labels``), since it changes only generated workflows.
The input ``mode`` of ``regenerate-workflows.yml`` chooses what happens next:

- ``pr`` (default): the pull request waits for a human.
- ``auto-commit``: the bot pushes to the default branch instead;
  this needs the bot App on the bypass list of the branch protection, otherwise it falls back to the pull request.
- ``pr-auto-merge``: the bot enables `auto-merge <https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/automatically-merging-a-pull-request>`__ on the pull request,
  so it is merged once the required checks pass: squashed, or with the method the repository allows instead.
  This needs "Allow auto-merge" in the repository settings and required status checks on the default branch.
  If the default branch also requires a review, put the bot App on the bypass list of that ruleset in the mode "pull requests only"
  and keep the required status checks in a separate ruleset that it does not bypass.

So it is sufficient to change the manifest: once that is merged, the bot takes care of the rest.

.. note::

   GitHub runs a `workflow_run <https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run>`__ workflow from the **default** branch.
   A change to ``trigger-downstream.yml`` therefore takes effect only once it is merged;
   on its own branch, the old graph is still used.
   Only setting a label runs the pull request's own copy.

When to run it
--------------

Downstream CI can be costly:
a change in a package low in the stack, such as ``ecbuild``, builds most of the stack again.
Many pull requests, e.g. to documentation or CI, cannot break a consumer,
so a pull request fans out only when it asks for it, with exactly one label:

- ``run-downstream-ci:all`` builds every consumer.
- ``run-downstream-ci:<n>`` builds the consumers up to level ``n``.
  From ``ecbuild``, ``eckit`` and ``ecflow`` are level 1, and ``eccodes`` is level 2 because it needs ``eckit``.
  A level cut never builds a package without the packages it needs.

A label added later starts the downstream CI by itself, without running ``CI`` again,
as long as ``CI`` already succeeded for the pull request's head commit.
If ``CI`` is still running, the downstream CI starts once it has finished.
Other labels start nothing.
The label persists, and will trigger the requested runs upon future pushes.
The downstream workflow can also be started by hand for a pull request, with a level instead of a label,
see :doc:`fork-pull-requests`.

To merge, a pull request needs exactly one ``run-downstream-ci:*`` label
or the explicit opt-out ``downstream-ci-not-needed``;
the lanes' statuses ``downstream/runner`` and ``downstream/hpc-atos`` are the required checks.
Until a pull request decides, nothing posts them, so they block as *Expected*.
:action:`post-downstream-decision` sets them to success for the opt-out,
and to pending as soon as a level label is set, until the downstream CI reports its result on them.
A lane without consumers is set to success on every event, whatever the labels.
The ``hpc-atos`` lane also follows the HPC labels (see :doc:`ci-yml`):
``hpc-ci-not-needed`` alone sets it to success, and a level label without ``run-hpc-ci`` leaves it *Expected*.
The generator writes it as ``pr-label-downstream-ci.yml`` into every repository,
so the required checks are the same everywhere and need no update when consumers come or go.
It runs on ``pull_request_target``, which is safe since it only reads the labels from the event:
the pull request cannot change its own gate, and a fork's pull request gets the statuses too.
It also starts the downstream CI for a level label, with :action:`dispatch-downstream`:
``trigger-downstream*.yml`` has no ``pull_request`` trigger of its own,
so that its runs, every job skipped for any other label, do not list on the pull request.

The labels only decide which jobs of the dependency graph are **skipped**.
It is also possible to restrict the full downstream dependency graph from this repository, i.e. what ``run-downstream-ci:all`` runs.
This is done in the :doc:`../reference/manifest` via

.. code:: toml

   [downstream]
   exclude = ["ecflow"]

Every consumer that depends on an excluded one is left out as well.
For example, excluding ``earthkit-data`` and ``earthkit-utils`` would also drop every package built on them.
