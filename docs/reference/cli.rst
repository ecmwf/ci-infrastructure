.. SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
..
.. SPDX-License-Identifier: Apache-2.0

Command-line tools
==================

The actions call these; run them by hand to debug a step locally.

.. click:: ci_infrastructure.generate_downstream_ci:main
   :prog: ci-infrastructure-generate

.. click:: ci_infrastructure.resolve_deps:main
   :prog: ci-infrastructure-resolve

.. click:: ci_infrastructure.fetch_deps:main
   :prog: ci-infrastructure-fetch

.. click:: ci_infrastructure.check_artifact:main
   :prog: ci-infrastructure-check

.. click:: ci_infrastructure.print_dep_table:main
   :prog: ci-infrastructure-print-dep-table

.. click:: ci_infrastructure.s3_store:main
   :prog: ci-infrastructure-s3
   :nested: full

.. click:: ci_infrastructure.hpc.orchestrate:main
   :prog: ci-infrastructure-hpc
   :nested: full

ci-infrastructure-check-ci-approval
-----------------------------------

The pre-commit hook; stdlib only, see `src/ci_infrastructure/check_ci_approval.py`.

ci-infrastructure-check-declaration
-----------------------------------

Local debugging of :action:`check-pr-declaration`; stdlib only, see
`src/ci_infrastructure/check_pr_declaration.py`.
