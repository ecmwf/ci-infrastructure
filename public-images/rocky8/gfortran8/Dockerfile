# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rocky8-gfortran8
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM eccr.ecmwf.int/public-ci-images/rocky8-base:latest

# Rocky 8's OWN gcc, 8.5, not an SCL toolset under /opt/rh. That is the compiler
# this platform exists to test: it is the distro default, and it is what the
# Atos HPC GNU builds target, so a rocky 8 leg on anything newer tests a
# combination nobody ships. The base already installs gcc and gcc-c++; only
# Fortran is missing from it.
RUN dnf -y install gcc-gfortran \
 && dnf clean all && rm -rf /var/cache/dnf

# ubuntu and debian name compilers <tool>-<version>; the rocky RPMs do not, so
# `g++-8` -- what every manifest asks CMake for -- resolves nowhere. Match the
# convention, and keep the version in the artifact name, where a compiler bump
# invalidates the cache on its own.
RUN for t in gcc g++ gfortran cpp; do \
      ln -s /usr/bin/$t /usr/local/bin/$t-8; \
    done

# Self-description. The same facts build-image.sh writes as OCI labels, baked in
# as environment as well: a label can only be read from OUTSIDE the image
# (registry or daemon), and a job running INSIDE it has neither.
# announce-image.sh and actions/announce-image print these. See IMAGES.md.
#
# Declared LAST so a new value never busts the cache of the layers above, and
# repeated in EVERY image rather than inherited: ENV is inherited by descendants,
# so a variant that did not re-declare these would announce itself as its base.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
