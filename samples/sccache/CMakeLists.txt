# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

# The smallest CMake project that still exercises a real C++ compile.
#
# It exists only so scripts/probe_sccache.py has something to build twice: the
# second build must be served from the object store, which is what proves the
# sccache S3 backend actually stores and retrieves. Keep it to one translation
# unit -- the point is the cache round trip, not the compile.
#
# CMake rather than a bare `sccache c++ -c` because CMAKE_CXX_COMPILER_LAUNCHER
# is the integration actions/setup-sccache documents to its callers, so this
# tests the path they are told to use.

cmake_minimum_required(VERSION 3.16)
project(sccache_smoke CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(sccache-smoke hello.cpp)
