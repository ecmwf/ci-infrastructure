<!--
SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)

SPDX-License-Identifier: Apache-2.0
-->


# Letting fork pull requests onto self-hosted hardware

What is at risk here is the hardware and the credentials that reach it — an HPC
account, a GPU node, a registry robot, an object store key. A `pull_request` run
from a fork gets none of them, which is why jobs that need them do not merely
fail for outside contributors, they cannot work at all. `pull_request_target`
hands them over — to anyone who opens a pull request, the moment the job checks
their branch out and runs it.
{action}`require-ci-approval` is what
makes that trade payable: the branch runs only after someone with write access
has read the diff and applied `approved-for-ci`.

```yaml
jobs:
  ci-approval:
    # bash, jq, and gh only to spend the label — no checkout, no Python, no
    # network to reach a verdict, so the cheapest runner is the right one. The
    # action says which tool is missing if an image turns out not to carry one.
    runs-on: ubuntu-slim
    permissions:
      pull-requests: write   # only so the label can be deleted
    steps:
      - uses: ecmwf/ci-infrastructure/actions/require-ci-approval@main

  build-on-hpc:
    needs: ci-approval
    runs-on: hpc
    ...
```

The step succeeds exactly when the gated jobs may run, so `needs:` is the whole
wiring — no `if:` on the dependants. Two properties are the point, and both are
easy to lose by rewriting this into something that looks equivalent:

- **It fails; it does not skip.** The obvious spelling — `if: contains(labels,
  'approved-for-ci')` on each job — is wrong, because a job skipped by a
  conditional reports *Success* to the merge box. An unapproved pull request
  would show a row of green ticks meaning "these never ran", and a required
  status check on them would enforce nothing.
- **The label is a single-use token.** It is deleted the moment it is honoured,
  so one approval buys one run and a contributor cannot earn approval on a
  harmless diff and then replay it. Deleting it *then*, rather than on the next
  push, is the whole point: consumers set `cancel-in-progress`, so a revocation
  that waits for one particular run to reach its own gate step is one a
  superseding run can cancel away. A `synchronize` or `reopened` still fails —
  and still deletes — as the backstop for exactly that case, so the caller must
  listen for `synchronize`.

  The cost is that approval covers a run, not a commit: a GitHub *re-run* replays
  the frozen event payload and still sees the label, but any new event needs a
  fresh one. On a fork pull request that also wants the downstream fan-out, apply
  `approved-for-ci` and `run-downstream-CI` together — the second label
  re-triggers CI, and that run needs an approval of its own.

It answers "may this contributor's code run on our hardware?", never "is this
job worth running on this pull request?". Opt-in labels, paths filters and
similar policy stay in the consuming repository, on the **gate job's own `if:`**
— skipping the gate skips everything behind it, which is the right outcome when
the jobs were not wanted anyway. The one thing that must not happen is that
condition being folded into a per-job `if:` that also subsumes the approval
decision.

Two things it cannot do for you. Applying a label needs only *triage*
permission, so a repository that hands triage to people it would not hand an
HPC account has widened the gate — treat "who may label" as "who may approve".
And a `pull_request_target` workflow always runs the base branch's copy of
itself, so a pull request editing the gated workflow cannot test that edit; use
`workflow_dispatch` on the branch.
