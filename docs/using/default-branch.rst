.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Changing the default branch
===========================

We want to have one default branch (``main|master|develop`` depending on the repo)
that is assumed and tested to be green.
**Nothing shall change the default branch except through a pull request.**
This holds also for a repository with a single maintainer, where nobody reviews the pull request:
the pull request is what guarantees that the CI runs.

The CI of basically every repository finishes in less than 5 minutes, the time to grab a cup of tea or coffe.
It ensures that the code is linted, compiles, the own tests pass, and the own guarantees are fulfilled.
Waiting is a very small price for knowing that the minimal checks pass.
A direct push that saves these 5 minutes and breaks the build costs far more in debugging afterwards.

Worse, a broken default branch breaks **every package** that builds on it.
Their developers then debug a failure that is not theirs,
which collectively costs far more than 5 minutes of one developer's time.

Whether a pull request also builds and tests its downstream consumers is described in :doc:`downstream-ci`.
