.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Describe your package
=====================

Every repository in the graph describes itself in ``.ci/manifest.toml``.

- ``[package]``: name, artifact prefix, repository, visibility
- ``[[deps]]``: upstream repositories, their default ``ref`` and ``compiler-inputs``
- ``[matrix.build]``: the build kind, its ``triggers``, ``needs`` and ``ctest``
- ``[matrix.build.defaults]`` and ``[[matrix.build.include]]``: one leg per toolchain
- which fields enter the artifact name, see :doc:`../reference/manifest`
