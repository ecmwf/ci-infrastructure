<!--
SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)

SPDX-License-Identifier: Apache-2.0
-->

# Runner-side hooks

Nothing here is baked into an image or fetched by a workflow. It is configuration
for the ARC runners themselves, kept next to the images so the two stay in step.

## `container-hook-wrapper.js`

Names the job's container image in the runner's own **"Initialize containers"**
block, above every workflow step.

On a GitHub-hosted runner that block prints the image and its digest. Under ARC's
Kubernetes mode it prints three lines of boilerplate:

```
##[group]Run '/home/runner/k8s/index.js'
shell: /home/runner/externals/node20/bin/node {0}
##[endgroup]
```

— which makes it the one place a job does *not* say what it is running in. That
`index.js` is the container hook, so pointing the hook at this wrapper restores
the line.

### Wiring

Mount or bake the file into the runner container, then in the scale-set values
set it as the hook entrypoint:

```yaml
template:
  spec:
    containers:
      - name: runner
        env:
          - name: ACTIONS_RUNNER_CONTAINER_HOOKS
            value: /home/runner/hooks/container-hook-wrapper.js
          # Where the wrapper delegates. Defaults to the Kubernetes hook below;
          # set it explicitly if the real hook ever moves, or to the docker hook.
          - name: CI_REAL_CONTAINER_HOOK
            value: /home/runner/k8s/index.js
```

**Roll out to one scale set first.** This runs at the start of every job on a
scale set that enables it, including other teams' — a broken hook fails them all
at "Initialize containers". Check that the block gained its line and that job
start time is unchanged before widening.

### What it does and does not print

Only what the hook payload already carries: `args.container.image`, plus each
`args.services[].image`. Hooks block job start and the runner applies no timeout
to them, so there is no registry lookup and no `kubectl` here. That also rules
out the digest and the `CI_IMAGE_*` provenance — at `prepare_job` the pod does
not exist, and those values are baked inside an image that has not started. The
image announces those itself once it is running; see *Self-description* in
[IMAGES.md](../IMAGES.md).

It is a pass-through, not a `prepare_job` handler: the runner allows no partial
opt-in (see [ADR 1891](https://github.com/actions/runner/blob/main/docs/adrs/1891-container-hooks.md)
— the handler must implement every command), so it prints, then replays the
payload to the real hook and exits with its status. `responseFile` is left
entirely to the real hook.

Tested by `tests/test_container_hook_wrapper.py`, which pins the pass-through
properties: the real hook receives the payload byte-identically and its exit code
survives, even when the payload is unparseable.
