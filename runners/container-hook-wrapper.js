// SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
//
// SPDX-License-Identifier: Apache-2.0
//
// ACTIONS_RUNNER_CONTAINER_HOOKS wrapper: names the job's container image in the
// runner's own "Initialize containers" block, above every workflow step. See
// runners/README.md.
//
// A GitHub-hosted runner prints the image and digest there. ARC's Kubernetes
// mode prints three lines of boilerplate and nothing else, so today that block
// is the one place a job does NOT say what it is running in.
//
// Pass-through, not a prepare_job handler: the runner allows no partial opt-in
// (actions/runner, docs/adrs/1891-container-hooks.md -- the handler must
// implement every command), so this prints and then delegates.
//
// JS rather than sh because the runner invokes a .js hook with its own bundled
// node; `node` is not on PATH in the runner container, but process.execPath is
// always the interpreter already running this file.

const payload = require('fs').readFileSync(0, 'utf8')

// Only what the payload already carries. Hooks block job start and the runner
// applies no timeout to them, so there is no registry lookup and no kubectl
// here -- which also rules out the digest and CI_IMAGE_*: the pod does not
// exist yet, and those are baked inside an image that has not started. The
// image's own announcer covers that provenance once the job is running.
try {
  const msg = JSON.parse(payload)
  if (msg.command === 'prepare_job') {
    const args = msg.args || {}
    if (args.container && args.container.image) {
      console.log(`job container: ${args.container.image}`)
    }
    for (const service of args.services || []) {
      if (service.image) console.log(`service container: ${service.image}`)
    }
  }
} catch {
  // Logging must never be the reason a job fails to start.
}

// responseFile is deliberately untouched -- the real hook owns that protocol.
const real = process.env.CI_REAL_CONTAINER_HOOK || '/home/runner/k8s/index.js'
const result = require('child_process').spawnSync(process.execPath, [real], {
  input: payload,
  stdio: ['pipe', 'inherit', 'inherit'],
})
process.exit(result.status === null ? 1 : result.status)
