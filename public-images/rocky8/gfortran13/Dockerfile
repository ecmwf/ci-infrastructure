# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rocky8-gfortran13
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM eccr.ecmwf.int/public-ci-images/rocky8-base:latest

# Toolchain layered on the shared base, which already provides sudo, git, curl,
# cmake, gh, python3.12, openssl-devel, etc.
#
# rocky 8's own gcc is 8.5. Anything newer arrives as an SCL "toolset" under
# /opt/rh rather than on the default PATH, which is the one real difference from
# the ubuntu variants, where `gfortran-13` is just a package.
RUN dnf -y install gcc-toolset-13 \
 && dnf clean all && rm -rf /var/cache/dnf

# Put the toolset on PATH directly rather than relying on its `enable` script.
#
# The usual recipe is `scl enable gcc-toolset-13` or sourcing
# /opt/rh/gcc-toolset-13/enable from BASH_ENV, but both only fire for a shell
# that reads them: BASH_ENV is honoured by non-interactive bash (so GitHub's
# `bash -e {0}` steps would work) and ignored by `sh -c`, which is what
# `docker run`, RUN lines and any `shell: sh` step use. Setting the two variables
# here means the toolchain is found the same way no matter who starts the
# process, which is the only property worth having in a CI image.
ENV PATH=/opt/rh/gcc-toolset-13/root/usr/bin:$PATH
ENV LD_LIBRARY_PATH=/opt/rh/gcc-toolset-13/root/usr/lib64:/opt/rh/gcc-toolset-13/root/usr/lib
