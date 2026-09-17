# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

# Built twice by scripts/probe_sccache.py; the second build must be a cache hit.

cmake_minimum_required(VERSION 3.16)
project(sccache_smoke CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(sccache-smoke hello.cpp)
