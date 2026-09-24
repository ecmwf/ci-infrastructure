.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Runners
=======

Runner groups
-------------

The following runner groups for the CI exist on the self-hosted Kubernetes cluster.

.. list-table::
   :header-rows: 1

   * - ``runs-on``
     - CPU (request / limit)
     - memory (request / limit)
     - disk
   * - ``arc-runner-normal``
     - 2 / 4
     - 2 / 8 GiB
     - 50 GiB
   * - ``arc-runner-large``
     - 4 / 8
     - 8 / 16 GiB
     - 50 GiB
   * - ``arc-runner-very-large``
     - 4 / 16
     - 8 / 32 GiB
     - 50 GiB
   * - ``arc-hpc-pet-vsphere-prod`` or ``hpc-submit``
     - —
     - —
     - 50 GiB

The HPC is reached through the runner class ``hpc-submit``.
It resolves to ``arc-hpc-pet-vsphere-prod``.
These runners are small and cheap and are not meant for heavy lifting.
They ship sources and dependencies to the HPC and submit the job to the queue.
Afterwards they copy the artifacts back and store it in S3.
See :doc:`../howto/hpc`.
To access the HPC you need to run on the HPC runner group and also need an HPC image (see below).

If you suspect a problem with the runners, trigger
`smoke-test-runners.yml <https://github.com/ecmwf/ci-infrastructure/actions/workflows/smoke-test-runners.yml>`__.
It checks each group's S3 object-store and sccache access.

`GitHub-hosted runners <https://docs.github.com/en/actions/reference/runners/github-hosted-runners>`__
can incur a cost.
It depends on the kind of runner and the visibility of the repository.
Public repos run for free on small Linux runners.
Prefer the self-hosted Kubernetes cluster.

A fork pull request shall reach the self-hosted runners only through :action:`require-ci-approval`.

Images
------

- You can use your own images, for example from `Docker Hub <https://hub.docker.com>`__
  or ECMWF's `ECCR Harbor <https://eccr.ecmwf.int>`__.
- There is also a list of official images with typical compiler toolchains.
  They are hosted at
  `public-ci-images <https://eccr.ecmwf.int/harbor/projects/548/repositories>`__
  (``eccr.ecmwf.int/public-ci-images/<name>:<tag>``) and
  `private-ci-images <https://eccr.ecmwf.int/harbor/projects/549/repositories>`__
  (``eccr.ecmwf.int/private-ci-images/<name>:<tag>``).

The public images need no authentication.
The private images are needed to connect to the HPC and require credentials.
Accessing the HPC requires an HPC image **and** the HPC runner group (see above).

The official images have ci-infrastructure baked in.
:action:`ensure-infrastructure-present` then reuses it and installs nothing.
Right after a merge to ``main`` the baked copy is stale until the images are republished.
The action then warns and installs from the checkout.

Your own images work too, but every job pip-installs ci-infrastructure into a venv.
That needs Python >= 3.11.4 in the image and outbound access to PyPI and GitHub.
To skip the install, build ``FROM`` an official ``base`` image.
Re-declare its ``CI_IMAGE_*`` block, as every official image does (follow the links to the base images in the table below).

To add/modify a public image, open a PR in ci-infrastructure.
Add it under `public-images/ <https://github.com/ecmwf/ci-infrastructure/tree/main/public-images>`__.
The ``base`` image of each platform shows what an image is expected to supply.
The rules for images are in
`public-images/README.md <https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/README.md>`__.
To add/modify a private image, contact the maintainers of `ci-infrastructure <https://github.com/ecmwf/ci-infrastructure>`__ .

Currently the following public images are supported.
Each is pulled as ``eccr.ecmwf.int/public-ci-images/<image>:latest``.

.. table::
   :class: grouped

   +------------------+-------------------------------------------+
   | Platform         | Image                                     |
   +==================+===========================================+
   | ``ubuntu22.04``  | `ubuntu22.04-base`_                       |
   |                  +-------------------------------------------+
   |                  | `ubuntu22.04-gcc11-gfortran11`_           |
   |                  +-------------------------------------------+
   |                  | `ubuntu22.04-gcc11-gfortran11-boost-qt6`_ |
   +------------------+-------------------------------------------+
   | ``ubuntu24.04``  | `ubuntu24.04-base`_                       |
   |                  +-------------------------------------------+
   |                  | `ubuntu24.04-gcc13-gfortran13`_           |
   |                  +-------------------------------------------+
   |                  | `ubuntu24.04-clang18`_                    |
   |                  +-------------------------------------------+
   |                  | `ubuntu24.04-gcc13-gfortran13-boost-qt6`_ |
   +------------------+-------------------------------------------+
   | ``ubuntu26.04``  | `ubuntu26.04-base`_                       |
   |                  +-------------------------------------------+
   |                  | `ubuntu26.04-gcc15-gfortran15`_           |
   |                  +-------------------------------------------+
   |                  | `ubuntu26.04-gcc15-gfortran15-boost-qt6`_ |
   +------------------+-------------------------------------------+
   | ``rocky8``       | `rocky8-base`_                            |
   |                  +-------------------------------------------+
   |                  | `rocky8-gcc8-gfortran8`_                  |
   |                  +-------------------------------------------+
   |                  | `rocky8-gcc8-gfortran8-boost-qt5`_        |
   +------------------+-------------------------------------------+
   | ``rocky9``       | `rocky9-base`_                            |
   |                  +-------------------------------------------+
   |                  | `rocky9-gcc11-gfortran11`_                |
   |                  +-------------------------------------------+
   |                  | `rocky9-gcc11-gfortran11-boost-qt5`_      |
   +------------------+-------------------------------------------+
   | ``rocky10``      | `rocky10-base`_                           |
   |                  +-------------------------------------------+
   |                  | `rocky10-gcc14-gfortran14`_               |
   |                  +-------------------------------------------+
   |                  | `rocky10-gcc14-gfortran14-boost-qt6`_     |
   +------------------+-------------------------------------------+
   | ``debian11``     | `debian11-base`_                          |
   |                  +-------------------------------------------+
   |                  | `debian11-gcc10-gfortran10`_              |
   |                  +-------------------------------------------+
   |                  | `debian11-gcc10-gfortran10-boost-qt5`_    |
   +------------------+-------------------------------------------+
   | ``debian12``     | `debian12-base`_                          |
   |                  +-------------------------------------------+
   |                  | `debian12-gcc12-gfortran12`_              |
   |                  +-------------------------------------------+
   |                  | `debian12-gcc12-gfortran12-boost-qt6`_    |
   +------------------+-------------------------------------------+
   | ``debian13``     | `debian13-base`_                          |
   |                  +-------------------------------------------+
   |                  | `debian13-gcc14-gfortran14`_              |
   |                  +-------------------------------------------+
   |                  | `debian13-gcc14-gfortran14-boost-qt6`_    |
   +------------------+-------------------------------------------+
   | ``fedora43``     | `fedora43-base`_                          |
   |                  +-------------------------------------------+
   |                  | `fedora43-gcc15-gfortran15`_              |
   |                  +-------------------------------------------+
   |                  | `fedora43-gcc15-gfortran15-boost-qt6`_    |
   +------------------+-------------------------------------------+
   | ``fedora44``     | `fedora44-base`_                          |
   |                  +-------------------------------------------+
   |                  | `fedora44-gcc16-gfortran16`_              |
   |                  +-------------------------------------------+
   |                  | `fedora44-gcc16-gfortran16-boost-qt6`_    |
   +------------------+-------------------------------------------+
   | ``rolling-arch`` | `rolling-arch-base`_                      |
   |                  +-------------------------------------------+
   |                  | `rolling-arch-gcc-gfortran`_              |
   |                  +-------------------------------------------+
   |                  | `rolling-arch-gcc-gfortran-boost-qt6`_    |
   +------------------+-------------------------------------------+

.. _ubuntu22.04-base: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/ubuntu22.04/base/Dockerfile
.. _ubuntu22.04-gcc11-gfortran11: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/ubuntu22.04/gcc11-gfortran11/Dockerfile
.. _ubuntu22.04-gcc11-gfortran11-boost-qt6: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/ubuntu22.04/gcc11-gfortran11-boost-qt6/Dockerfile
.. _ubuntu24.04-base: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/ubuntu24.04/base/Dockerfile
.. _ubuntu24.04-gcc13-gfortran13: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/ubuntu24.04/gcc13-gfortran13/Dockerfile
.. _ubuntu24.04-clang18: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/ubuntu24.04/clang18/Dockerfile
.. _ubuntu24.04-gcc13-gfortran13-boost-qt6: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/ubuntu24.04/gcc13-gfortran13-boost-qt6/Dockerfile
.. _ubuntu26.04-base: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/ubuntu26.04/base/Dockerfile
.. _ubuntu26.04-gcc15-gfortran15: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/ubuntu26.04/gcc15-gfortran15/Dockerfile
.. _ubuntu26.04-gcc15-gfortran15-boost-qt6: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/ubuntu26.04/gcc15-gfortran15-boost-qt6/Dockerfile
.. _rocky8-base: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/rocky8/base/Dockerfile
.. _rocky8-gcc8-gfortran8: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/rocky8/gcc8-gfortran8/Dockerfile
.. _rocky8-gcc8-gfortran8-boost-qt5: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/rocky8/gcc8-gfortran8-boost-qt5/Dockerfile
.. _rocky9-base: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/rocky9/base/Dockerfile
.. _rocky9-gcc11-gfortran11: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/rocky9/gcc11-gfortran11/Dockerfile
.. _rocky9-gcc11-gfortran11-boost-qt5: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/rocky9/gcc11-gfortran11-boost-qt5/Dockerfile
.. _rocky10-base: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/rocky10/base/Dockerfile
.. _rocky10-gcc14-gfortran14: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/rocky10/gcc14-gfortran14/Dockerfile
.. _rocky10-gcc14-gfortran14-boost-qt6: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/rocky10/gcc14-gfortran14-boost-qt6/Dockerfile
.. _debian11-base: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/debian11/base/Dockerfile
.. _debian11-gcc10-gfortran10: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/debian11/gcc10-gfortran10/Dockerfile
.. _debian11-gcc10-gfortran10-boost-qt5: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/debian11/gcc10-gfortran10-boost-qt5/Dockerfile
.. _debian12-base: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/debian12/base/Dockerfile
.. _debian12-gcc12-gfortran12: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/debian12/gcc12-gfortran12/Dockerfile
.. _debian12-gcc12-gfortran12-boost-qt6: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/debian12/gcc12-gfortran12-boost-qt6/Dockerfile
.. _debian13-base: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/debian13/base/Dockerfile
.. _debian13-gcc14-gfortran14: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/debian13/gcc14-gfortran14/Dockerfile
.. _debian13-gcc14-gfortran14-boost-qt6: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/debian13/gcc14-gfortran14-boost-qt6/Dockerfile
.. _fedora43-base: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/fedora43/base/Dockerfile
.. _fedora43-gcc15-gfortran15: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/fedora43/gcc15-gfortran15/Dockerfile
.. _fedora43-gcc15-gfortran15-boost-qt6: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/fedora43/gcc15-gfortran15-boost-qt6/Dockerfile
.. _fedora44-base: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/fedora44/base/Dockerfile
.. _fedora44-gcc16-gfortran16: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/fedora44/gcc16-gfortran16/Dockerfile
.. _fedora44-gcc16-gfortran16-boost-qt6: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/fedora44/gcc16-gfortran16-boost-qt6/Dockerfile
.. _rolling-arch-base: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/rolling-arch/base/Dockerfile
.. _rolling-arch-gcc-gfortran: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/rolling-arch/gcc-gfortran/Dockerfile
.. _rolling-arch-gcc-gfortran-boost-qt6: https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/rolling-arch/gcc-gfortran-boost-qt6/Dockerfile

The one private image is ``eccr.ecmwf.int/private-ci-images/ubuntu24.04-internal-tools``.
It lives in `ecmwf/ci-container-images <https://github.com/ecmwf/ci-container-images>`__.
