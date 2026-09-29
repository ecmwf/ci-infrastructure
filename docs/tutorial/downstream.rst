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
which are then prepared on sync branches of the same name (see :doc:`manifest`).

Declaring the graph
-------------------

The producer lists the repositories to build after a change, eckit for example triggers eccodes:

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
eccodes' ``build`` waits for the ``build`` kinds of ecbuild, stack-dependencies and eckit,
its ``build-hpc`` for their ``build-hpc``.
``needs`` is written out only for an order within one repository, such as ``needs = ["build"]`` for a test kind,
or to choose between two kinds of a producer that publish the same package.
The downstream graph is a subset of the dependency graph (in the other direction):
every repository in ``[[trigger-downstream]]`` must list the producer in its ``[[deps]]``,
but not every dependency has to trigger its consumers.
The graph is transitive as well, so a change in ecbuild also reaches eccodes through eckit.
:doc:`ci-infrastructure-generate <../reference/cli/ci-infrastructure-generate>` checks these rules; see :doc:`../reference/manifest`.

The generated workflows
-----------------------

Running :doc:`ci-infrastructure-generate <../reference/cli/ci-infrastructure-generate>` in the repository writes two workflows, which are committed with it:
``trigger-downstream.yml`` in the producer and ``cross-repo-trigger.yml`` in the consumer,
each with an ``-hpc`` variant for HPC kinds.
Once the workflow ``CI`` of a pull request has completed successfully,
``trigger-downstream.yml`` builds every consumer at its ``ref`` against the commit under test,
in dependency order,
and posts the result as the status ``downstream/runner`` (and ``downstream/hpc``) on the pull request.

The generated files must follow the manifests, also those of the other repositories.
:action:`validate-generated-workflows` reports when they have drifted,
and :action:`regenerate-workflows-pr` opens a pull request with the regenerated files.

.. note::

   GitHub runs a `workflow_run <https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run>`__ workflow from the **default** branch.
   A change to ``trigger-downstream.yml`` therefore takes effect only once it is merged;
   on its own branch, the old graph is still used.

When to run it
--------------

Downstream CI can be costly:
a change in a package low in the stack, such as ecbuild, builds most of the stack again.
Many pull requests, e.g. to documentation or CI, cannot break a consumer,
so a pull request fans out only when it asks for it, with exactly one label:

- ``run-downstream-ci:all`` builds every consumer.
- ``run-downstream-ci:<n>`` builds the consumers up to level ``n``.
  A consumer's level is one more than the highest level among the consumers it depends on;
  from ecbuild, eckit and ecflow are level 1, and eccodes, which needs eckit, is level 2.
  A level cut therefore never builds a package without the packages it needs.

The scheme is the same in every repository.
:action:`check-pr-label` reads the label when ``CI`` completes;
since ``ci.yml`` also runs on ``labeled``, adding the label later starts a fresh ``CI`` run by itself.
A push to one of the branches for which ``ci.yml`` runs on ``push``, typically the default branch after a merge,
always fans out completely; there is no pull request to carry a label.

To make the decision explicit, the workflow `pr-label-downstream-ci.yml <https://github.com/ecmwf/eckit/blob/develop/.github/workflows/pr-label-downstream-ci.yml>`__
runs :action:`require-label-decision`.
It keeps the status ``downstream-ci-label`` pending
until the pull request carries a level label or ``downstream-ci-not-needed``,
and turns it red on the bare ``run-downstream-ci``, an invalid level or two level labels.
Made a required check, it prevents a merge without a decision.

A producer can also leave consumers out of its fan-out for good, in ``[downstream]``:

.. code:: toml

   [downstream]
   exclude = ["ecflow"]

Every consumer that depends on an excluded one is left out as well, since it could not resolve that dependency;
excluding eckit from ecbuild's fan-out therefore also drops eccodes.
Other producers are unaffected: eckit's own downstream CI still builds eccodes.
See :doc:`../reference/manifest` for the field.
