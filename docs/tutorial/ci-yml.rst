.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Write your ci.yml
=================

First and foremost, a repository can always simply write its own CI.
ci-infrastructure does not replace GitHub Actions, it only adds building blocks to it;
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
The virtual environment keeps the project's packages apart from ci-infrastructure,
which is installed into the same interpreter.

The workflow's name matters
---------------------------

The name ``CI`` is not arbitrary.
Downstream CI starts once a workflow named exactly ``CI`` has completed successfully,
so a repository that wants to use the downstream CI has to use that name.
A repository that does not take part is free to name its workflows as it likes.

What comes next
---------------

Whatever a repository does in its ``ci.yml`` is up to it.
Only a repository that uses other ECMWF packages, or is used by them,
has to declare its contents and dependencies; this is the subject of :doc:`manifest`.
A change that spans several such repositories is tested with :doc:`feature-branches`.

Fork pull requests
------------------

A pull request from a fork runs this workflow as well, only without access to secrets.
On a self-hosted runner this means outside code on our hardware.
How to guard against this is the subject of :doc:`fork-pull-requests`.
