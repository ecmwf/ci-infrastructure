.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Connect downstream repositories
===============================

A change is tested against the repositories that depend on it.

- ``[[trigger-downstream]]`` in the producer, ``needs`` in the consumer
- ``ci-infrastructure-generate`` writes ``trigger-downstream.yml`` and ``cross-repo-trigger.yml``
- :action:`validate-generated-workflows` reports drift, :action:`regenerate-workflows-pr` fixes it
- downstream CI runs only with the ``run-downstream-CI`` label
- a missing artifact triggers a rebuild in its producer
