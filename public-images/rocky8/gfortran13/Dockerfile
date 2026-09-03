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

# ubuntu and debian name compilers <tool>-<version>; an SCL toolset does not, so
# `g++-13` -- what every manifest asks CMake for -- resolves nowhere. Match the
# convention, and keep the version in the artifact name, where a toolset bump
# invalidates the cache on its own.
RUN for t in gcc g++ gfortran cpp; do \
      ln -s /opt/rh/gcc-toolset-13/root/usr/bin/$t /usr/local/bin/$t-13; \
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
