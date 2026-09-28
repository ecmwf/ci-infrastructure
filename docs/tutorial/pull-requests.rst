.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Pull requests
=============

Two gates decide what a pull request may run.

- :action:`require-ci-approval`: a fork reaches the self-hosted runners only after approval,
  see :doc:`fork-pull-requests`
- :action:`require-label-decision`: every pull request decides about downstream CI
- the labels and when to set them
