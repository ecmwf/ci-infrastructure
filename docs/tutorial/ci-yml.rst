.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Write your ci.yml
=================

First and foremost you can always simply write your own CI.
&put good tutorial to Github actions&

A simple example for a python project would be.

name: CI
on:
    pull_request:

run on the normal runner group :doc:`../reference/runners`

announce-image
pre-commit:
pip install
pytest

name is necessary for downstream CI, it starts once a job CI finished successfully.
If you don't have it or want it, name freely.

pull request does not run for for fork pull requests.
refer to section fork pull requests.

run on the normal runner group :doc:`../reference/runners`

typical jobs in sequence.
use a checkout python as example.

Whatever you do in your CI is up to you.
Only if you want to use other ECMWF repos or to be used by them, you need to declare the contents and dependencies of your package.
For this see the manifest section next.




The ``ci.yml`` is written by hand and calls the actions in order.

- ``ci-approval`` and ``pre-commit`` first
- ``resolve``: :action:`checkout-under-test`, then :action:`resolve-deps` emits the matrix
- ``build``: :action:`fetch-deps`, your own build step or :action:`cmake-build`, ctest
- :action:`publish-artifact` uploads the install tree, :action:`print-dep-table` shows what was used
- :action:`announce-image` first in every job with a ``container:``
- the runner groups and images to choose from, see
