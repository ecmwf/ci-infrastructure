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

`ACTIONS_RUNNER_CONTAINER_HOOKS` names the executable the runner calls instead of
doing container work itself. Under `containerMode.type: kubernetes` the ARC chart
sets it to `/home/runner/k8s/index.js` — that is why "Initialize containers" reads
`Run '/home/runner/k8s/index.js'`. Pointing it at the wrapper instead puts a line
in that block; the wrapper then calls the same `index.js`, so nothing about how
jobs run changes.

**Overriding it is a supported path, not a fight with the chart.** The chart only
injects its default when you have not set the variable yourself
(`charts/gha-runner-scale-set/templates/_helpers.tpl`, in
`gha-runner-scale-set.kubernetes-mode-runner-container`):

```gotemplate
{{- if eq $env.name "ACTIONS_RUNNER_CONTAINER_HOOKS" }}
  {{- $setContainerHooks = 0 }}
{{- end }}
...
{{- if $setContainerHooks }}
  - name: ACTIONS_RUNNER_CONTAINER_HOOKS
    value: /home/runner/k8s/index.js
{{- end }}
```

So declaring it in `template.spec.containers[name: runner].env` replaces the
default cleanly — no duplicate env, and `containerMode.type: kubernetes` stays on.

Deliver the file either by baking it into the runner image, or — with no image
rebuild — from a ConfigMap in the runner namespace:

```sh
kubectl -n <runner-namespace> create configmap ci-container-hook \
  --from-file=container-hook-wrapper.js=runners/container-hook-wrapper.js
```

```yaml
# gha-runner-scale-set values
containerMode:
  type: kubernetes          # unchanged
template:
  spec:
    volumes:
      - name: ci-container-hook
        configMap:
          name: ci-container-hook
    containers:
      - name: runner
        image: ghcr.io/actions/actions-runner:latest
        command: ["/home/runner/run.sh"]
        volumeMounts:
          - name: ci-container-hook
            mountPath: /home/runner/hooks
            readOnly: true
        env:
          # Replaces the chart default above.
          - name: ACTIONS_RUNNER_CONTAINER_HOOKS
            value: /home/runner/hooks/container-hook-wrapper.js
          # What the wrapper delegates to. Optional -- this is its default.
          # Set it if the real hook moves, or to the docker-mode hook.
          - name: CI_REAL_CONTAINER_HOOK
            value: /home/runner/k8s/index.js
```

Redeploy the scale set; runners pick it up as pods recycle. To undo, drop the two
env entries and the chart's default returns.

**Roll out to one scale set first.** This runs at the start of every job on a
scale set that enables it, including other teams' — a broken hook fails them all
at "Initialize containers". Check that the block gained its line and that job
start time is unchanged before widening.

### Sources

- [Customizing the containers used by jobs](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/customize-containers)
  — what `ACTIONS_RUNNER_CONTAINER_HOOKS` is, and that the runner sends
  `prepare_job`, `cleanup_job`, `run_container_step` and `run_script_step`.
- [ADR 1891, container hooks](https://github.com/actions/runner/blob/main/docs/adrs/1891-container-hooks.md)
  — the stdin protocol (`command`, `responseFile`, `args`, `state`), that "all
  text written to stdout or stderr should appear in the job or step logs", and
  that a handler must implement every command.
- [actions/runner-container-hooks](https://github.com/actions/runner-container-hooks)
  — the k8s hook this wrapper delegates to;
  [`examples/prepare-job.json`](https://github.com/actions/runner-container-hooks/blob/main/examples/prepare-job.json)
  is the payload shape, with the image at `args.container.image`.
- [gha-runner-scale-set `values.yaml`](https://github.com/actions/actions-runner-controller/blob/master/charts/gha-runner-scale-set/values.yaml)
  — where the runner container env lives, and the kubernetes-mode defaults
  (`ACTIONS_RUNNER_CONTAINER_HOOKS`, `ACTIONS_RUNNER_POD_NAME`,
  `ACTIONS_RUNNER_REQUIRE_JOB_CONTAINER`).
- [Deploying runner scale sets with ARC](https://docs.github.com/en/actions/how-tos/manage-runners/use-actions-runner-controller/deploy-runner-scale-sets)
  — kubernetes mode, and the `template.spec.containers[name: runner].env` form.

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
