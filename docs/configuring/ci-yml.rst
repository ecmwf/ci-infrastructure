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
It is the last job of ``ci.yml`` and collects all others with :action:`ci-result`:

.. code:: yaml

   jobs:
     # ... pre-commit, test, ...

     ci-result:
       if: always()
       needs: [pre-commit, test]
       runs-on: ubuntu-slim
       steps:
         - uses: ecmwf/ci-infrastructure/actions/ci-result@main
           with:
             needs: ${{ toJSON(needs) }}

Without ``if: always()``, a failed job would skip ``ci-result``, and a skipped check counts as passed.
It also fails when no job succeeded: in a repository with ``ci-fork.yml``, a fork's own ``pull_request`` run skips everything,
so a maintainer merges such a pull request after checking ``ci-fork``'s result, bypassing the rule.
The ``check-ci-result`` pre-commit hook fails a ``ci.yml`` whose ``ci-result`` job lacks any of this,
or whose ``needs:`` does not list exactly the other jobs; it prints the line to paste.
A job that itself needs ``ci-result`` runs after it and is left out.

.. code:: yaml

   # .pre-commit-config.yaml
   - repo: https://github.com/ecmwf/ci-infrastructure
     rev: <commit>
     hooks:
       - id: check-ci-result

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
