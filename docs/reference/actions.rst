.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Actions
=======

Every action is a composite action, called as
`ecmwf/ci-infrastructure/actions/<name>@main`. This page is generated from the
`action.yml` files.

Reusable workflows
==================

.. autoworkflow:: .github/workflows/check-pr-declaration.yml

   Fails a pull request whose description does not end with the Contributor
   Declaration; see :doc:`../howto/contributor-declaration`.

Composite actions
=================

.. autoactions::
