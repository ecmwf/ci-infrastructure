.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Write your ci.yml
=================

First and foremost, a repository can always simply write its own CI.
``ci-infrastructure`` does not replace GitHub Actions, it only adds building blocks to it;
especially for reusing other artifacts from the ECMWF stack.
If GitHub Actions is new to you, start with
`Understanding GitHub Actions <https://docs.github.com/en/actions/get-started/understand-github-actions>`__
and keep the
`workflow syntax <https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax>`__
at hand.

A minimal example
-----------------

A small Python project could test itself with the following ``.github/workflows/ci.yml``:

.. code:: yaml

   name: CI

   on:
     push:
       branches: [main]
     pull_request:

   permissions:
     contents: read

   jobs:
     pre-commit:
       runs-on: ubuntu-slim
       steps:
         - uses: actions/checkout@v6
         - uses: ecmwf/ci-infrastructure/actions/pre-commit@main

     test:
       needs: pre-commit
       runs-on: arc-runner-normal
       container:
         image: eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest
       steps:
         - uses: ecmwf/ci-infrastructure/actions/announce-image@main
         - uses: actions/checkout@v6
         - run: |
             python3 -m venv .venv
             .venv/bin/pip install . pytest
             .venv/bin/python -m pytest

The jobs run in sequence, and the tests start only if :action:`pre-commit` succeeded.
It rejects formatting and lint errors cheaply,
on the small GitHub-hosted ``ubuntu-slim`` runner,
before the actual tests occupy a larger runner.
The tests run on the self-hosted ``arc-runner-normal`` group inside one of the official images;
the runner groups and images to choose from are listed in :doc:`../reference/runners`.
:action:`announce-image` comes first in every job with a ``container:``,
so that the log states which image the job ran in.
The virtual environment keeps the project's packages apart from ``ci-infrastructure``,
which is installed into the same interpreter.

The workflow's name matters
---------------------------

The name ``CI`` is not arbitrary.
Downstream CI starts once a workflow named exactly ``CI`` has completed successfully,
so a repository that wants to use the downstream CI has to use that name.
A repository that does not take part is free to name its workflows as it likes.

The required check
------------------

The default branch requires a single check, ``ci-result``,
so that renaming a job or a matrix leg never touches the branch rules.
It is a commit status that :action:`ci-result` posts from the last job of ``ci.yml``:

.. code:: yaml

   jobs:
     # ... pre-commit, test, ...

     ci-result:
       name: collect results
       if: always()
       needs: [test]
       runs-on: ubuntu-slim
       permissions:
         statuses: write
       steps:
         - uses: ecmwf/ci-infrastructure/actions/ci-result@main
           with:
             needs: ${{ toJSON(needs) }}

Every needed job must succeed; skipped counts as failed.
A failure therefore propagates: the jobs after it are skipped, and ``ci-result`` fails.
So ``needs:`` may list just the end jobs, the ones no other job needs.
Only a job with ``if: always()``, ``!cancelled()`` or ``failure()`` runs despite a failure before it;
list what it needs as well if that should count.
Without ``if: always()`` on the job itself, a failed job would skip it, and nothing would be posted.

The job's own check run is named ``collect results``, never ``ci-result``, so that only the status counts.
A fork's own ``pull_request`` run posts nothing.
In a repository with ``ci-fork.yml``, its call of ``ci.yml`` posts ``ci-result`` onto the fork's commit;
elsewhere the check stays expected until a maintainer merges, bypassing the rule.

The ``check-ci-result`` pre-commit hook checks that the job exists, runs ``if: always()``
with ``statuses: write`` and ``name: collect results``, needs at least one job,
and passes ``toJSON(needs)`` to the action.

.. code:: yaml

   # .pre-commit-config.yaml
   - repo: https://github.com/ecmwf/ci-infrastructure
     rev: <commit>
     hooks:
       - id: check-ci-result

HPC legs
--------

Legs that build on the HPC (see :doc:`hpc`) are scarce, so a pull request runs them only once it carries the label ``run-hpc-ci``,
or waives them with ``hpc-ci-not-needed``, just as ``downstream-ci-not-needed`` waives the downstream CI.
They report a second status, ``ci-result/hpc-atos``, which a repository with HPC legs also requires before merge.
:action:`hpc-gate` decides whether they run, and a second :action:`ci-result` job collects them:

.. code:: yaml

   jobs:
     hpc-gate:
       needs: ci-approval
       runs-on: ubuntu-slim
       permissions:
         statuses: write
         pull-requests: read
       outputs:
         run: ${{ steps.gate.outputs.run }}
       steps:
         - id: gate
           uses: ecmwf/ci-infrastructure/actions/hpc-gate@main

     wait-build-hpc:
       needs: [ci-approval, resolve, hpc-gate]
       if: needs.hpc-gate.outputs.run == 'true'
       # ...

     build-hpc:
       needs: [resolve, wait-build-hpc]
       # ...

     ci-result:
       needs: [hpc-gate, test]  # no HPC job
       # ...

     collect-result-hpc:
       name: collect HPC results
       if: always() && needs.hpc-gate.outputs.run == 'true'
       needs: [hpc-gate, wait-build-hpc, build-hpc]
       runs-on: ubuntu-slim
       permissions:
         statuses: write
       steps:
         - uses: ecmwf/ci-infrastructure/actions/ci-result@main
           with:
             needs: ${{ toJSON(needs) }}
             context: ci-result/hpc-atos

Outside a pull request the HPC legs always run.
On a pull request :action:`hpc-gate` lets them run with ``run-hpc-ci`` and sets ``ci-result/hpc-atos`` to pending until ``collect-result-hpc`` reports;
with ``hpc-ci-not-needed`` alone it sets it to success.
With neither label nothing posts it, so, like the downstream statuses, the required check blocks the merge as *Expected*.
The HPC legs never count towards ``ci-result``.

Adding ``run-hpc-ci`` later starts the HPC legs by itself, without running the rest of ``CI`` again.
The generator writes ``pr-label-hpc-ci.yml`` into every repository with HPC legs,
which runs :action:`hpc-label-decision` on a change of either label and whenever ``CI`` completes.
If the pull request's ``CI`` finished with its HPC legs skipped, it re-runs the job ``hpc-gate``,
and with it every job that needs it: the HPC legs, ``collect-result-hpc`` and ``ci-result``.
If ``CI`` is still running, it does so once ``CI`` has completed.
The gate reads the labels anew, since a re-run keeps the event of the run it repeats.
The downstream CI follows the re-run's completion; its ``runner`` lane skips such an attempt, whose runner legs did not run again.
Removing a label never resets the result of HPC legs that ran.

``check-ci-result`` also checks ``collect-result-hpc`` if it exists:
an ``if:`` with ``always()``, ``name: collect HPC results``, ``statuses: write``, at least one needed job,
and ``toJSON(needs)`` and ``context: ci-result/hpc-atos`` passed to the action.

What comes next
---------------

Whatever a repository does in its ``ci.yml`` is up to it.
Only a repository that uses other ECMWF packages, or is used by them,
has to declare its contents and dependencies; this is the subject of :doc:`manifest`.
A change that spans several such repositories is tested with :doc:`../using/feature-branches`.

Fork pull requests
------------------

A pull request from a fork runs this workflow as well, only without access to secrets.
On a self-hosted runner this means outside code on our hardware,
so :action:`require-ci-approval` stops it unless a maintainer approved it.
Even then it cannot build anything without the secrets.

A repository with ``fork-pull-requests = true`` (see :doc:`fork-pull-requests`)
runs an approved fork commit through ``ci-fork.yml``, which calls this workflow.
For that, ``ci.yml`` needs three changes:

.. code:: yaml

   on:
     # ... the existing triggers, unchanged ...
     workflow_call:

   jobs:
     ci-approval:
       # A fork's own pull_request run does nothing; ci-fork.yml runs this workflow instead.
       if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
       runs-on: ubuntu-slim
       permissions:
         pull-requests: write
       steps:
         - uses: ecmwf/ci-infrastructure/actions/require-ci-approval@main

     build:
       needs: [ci-approval, resolve]
       steps:
         # Not actions/checkout: under pull_request_target that is the base branch.
         - uses: ecmwf/ci-infrastructure/actions/checkout-under-test@main
           with:
             allow-unsafe-pr-checkout: ${{ github.event_name == 'pull_request_target' }}

- ``on:`` also has ``workflow_call``.
- The gate job is called ``ci-approval`` and skips a fork's ``pull_request`` run.
- Every checkout of the repository itself uses :action:`checkout-under-test` with exactly this ``allow-unsafe-pr-checkout``;
  checkouts of other repositories (with ``repository:``) may stay ``actions/checkout``.

The ``check-ci-approval`` pre-commit hook checks all three.
