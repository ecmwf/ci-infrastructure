.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Fork pull requests and CI approval
==================================

A pull request from a fork runs outside code on our self-hosted runners, close to the credentials they hold.
It therefore runs only after a maintainer has approved it.

- why a fork is different from a branch in the repository
- :action:`require-ci-approval` and the ``approved-for-ci`` label
- the approval is single-use: a new push needs a new approval
- the order of labels: ``run-downstream-ci:all`` or ``run-downstream-ci:<n>`` before CI finishes, ``approved-for-ci`` last
- ``pull_request`` today, ``pull_request_target`` as the end state
