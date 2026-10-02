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
which are then prepared on ``sync-branch/`` or ``feature/`` branches of the same name (see :doc:`../using/feature-branches`).

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
Other labels start nothing, and neither does a label on a pull request from a fork.
The label persists, and will trigger the requested runs upon future pushes.

To merge, a pull request needs exactly one ``run-downstream-ci:*`` label
or the explicit opt-out ``downstream-ci-not-needed``;
:action:`require-label-decision` enforces this.

The labels only decide which jobs of the dependency graph are **skipped**.
It is also possible to restrict the full downstream dependency graph from this repository, i.e. what ``run-downstream-ci:all`` runs.
This is done in the :doc:`../reference/manifest` via

.. code:: toml

   [downstream]
   exclude = ["ecflow"]

Every consumer that depends on an excluded one is left out as well.
For example, excluding ``earthkit-data`` and ``earthkit-utils`` would also drop every package built on them.
