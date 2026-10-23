#!/bin/sh
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
#
# Run inside an image (check-emudhra.yml): fails unless curl and uv reject both of
# the researcher's test hosts with a certificate error (curl: exit 60). The first chains to
# the emSign root, the second through Sectigo's AAA cross-sign. Any other failure
# proves nothing, hence the github.com control.
set -u

curl -fsS -o /dev/null --max-time 30 https://github.com \
  || { echo "::error::control: https://github.com unreachable, the probe proves nothing"; exit 1; }

pkg=$( (dpkg-query -W -f='${Version}' ca-certificates || rpm -q ca-certificates || pacman -Q ca-certificates-mozilla) 2>/dev/null | head -1)
echo "ca-certificates: $pkg"

status=0
for host in dv-g3b-emudhra.we-have-a-problem.com dv-g3b-aaa-emudhra.we-have-a-problem.com; do
  err=$(curl -sS -o /dev/null --max-time 30 "https://$host" 2>&1)
  rc=$?
  err=$(echo "$err" | head -1)
  case $rc in
    60) echo "$host: rejected (60) -- $err" ;;
    0) echo "::error::$host: ACCEPTED, the image trusts a compromised eMudhra CA"; status=1 ;;
    *) echo "::error::$host: failed with $rc, not a certificate rejection -- $err"; status=1 ;;
  esac
  # uv trusts its own compiled-in roots, not the image's trust store.
  out=$(uv pip install --dry-run --no-cache --python "$CI_INFRASTRUCTURE_PYTHON" --index-url "https://$host/simple/" foo 2>&1)
  if echo "$out" | grep -q "invalid peer certificate"; then
    echo "$host: rejected by $(uv --version)"
  else
    echo "::error::$host: not rejected by $(uv --version) -- $(echo "$out" | tail -1)"; status=1
  fi
done
exit $status
