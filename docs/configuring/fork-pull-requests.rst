.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Fork pull requests
==================

CI for pull requests from forks is deliberately not supported yet.
A fork's code is untrusted, and there are two ways in which it could do harm:

- it runs on ECMWF hardware, i.e. on the self-hosted runners and, through them, on the HPC;
- it reads the secrets of the workflow, e.g. the GitHub App key or the registry credentials.

The ``ci.yml`` therefore runs on ``pull_request``, which is preferred over ``pull_request_target``.
Under ``pull_request`` a fork's run gets neither secrets nor a token that can write,
whereas ``pull_request_target`` runs in the context of the base repository with both.
In addition, :action:`require-ci-approval` keeps a fork's run off the self-hosted runners
until a maintainer has reviewed the change and set the ``approved-for-ci`` label.
Setting a label does not start ``CI``, so the maintainer then re-runs it.
The downstream CI never starts for a fork's pull request.
No workflow checks out a fork's code in a trusted context:
``actions/checkout`` refuses this under ``pull_request_target`` and ``workflow_run``,
and nothing sets ``allow-unsafe-pr-checkout`` to override it.

If fork pull requests become relevant for your repository,
please open an `issue in ci-infrastructure <https://github.com/ecmwf/ci-infrastructure/issues>`__.
