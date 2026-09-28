.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Add an HPC build
================

An HPC build is a matrix kind with ``execution = "hpc"``.

- the repository owns its recipe, ``.ci/hpc/build.sh.j2``
- ``runs-on = "hpc-submit"``, the private ``internal-tools`` image
- :action:`build-on-hpc` in place of the build step
- the details are in :doc:`../howto/hpc`
