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

The generated workflows
-----------------------

The workflows that run downstream CI are auto-generated
by :doc:`ci-infrastructure-generate <../reference/cli/ci-infrastructure-generate>`.
Once the workflow ``CI`` of a pull request has completed successfully,
the downstream CI builds every consumer against the commit under test,
in dependency order,
and posts the result as the status ``downstream/runner`` (and ``downstream/hpc-atos``) on the pull request.

The generated files must follow the manifests, also those of the other repositories.
:action:`validate-generated-workflows` reports when they have drifted.
A bot runs :action:`regenerate-workflows-pr` on every push to the default branch and nightly,
and opens a pull request with the regenerated workflows.
It also pins the ``rev`` of the ci-infrastructure hooks in ``.pre-commit-config.yaml`` to the commit that generated them,
so the pull request is checked by the hooks that match its files.
It labels a new pull request ``downstream-ci-not-needed`` (the input ``labels``), since it changes only generated workflows.
The input ``mode`` of ``regenerate-workflows.yml`` chooses what happens next:

- ``pr`` (default): the pull request waits for a human.
- ``auto-commit``: the bot pushes to the default branch instead;
  this needs the bot App on the bypass list of the branch protection, otherwise it falls back to the pull request.
- ``pr-auto-merge``: the bot enables `auto-merge <https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/automatically-merging-a-pull-request>`__ on the pull request,
  so it is squash-merged once the required checks pass.
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
