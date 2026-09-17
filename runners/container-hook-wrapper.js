// SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
//
// SPDX-License-Identifier: Apache-2.0
//
// ACTIONS_RUNNER_CONTAINER_HOOKS wrapper: names the job's container image in the
// runner's own "Initialize containers" block, above every workflow step. See
// runners/README.md.
//
// Prints, then delegates to the real hook (no partial opt-in exists). JS so the
// runner's bundled node runs it; `node` is not on PATH.

const payload = require('fs').readFileSync(0, 'utf8')

// Payload only: hooks block job start with no timeout, so no lookups here.
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
