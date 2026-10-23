#!/bin/sh
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
#
# Run as root by every base image. Upgrades ca-certificates, then distrusts the
# roots the compromised eMudhra intermediates chain to (Oct 2026), as Red Hat's
# fix does, for distros that have not shipped one (rocky8, rocky10, EOL debian11).
# A no-op where the roots are already gone. check-emudhra-rejected.sh proves it.
set -eu

ROOTS="emSign ECC Root CA - G3
emSign Root TLS CA - G3
emSign Root CS CA - G3
AAA Certificate Services"

if [ -f /etc/ca-certificates.conf ]; then
  apt-get update
  apt-get install -y --only-upgrade ca-certificates
  rm -rf /var/lib/apt/lists/*
  # Debian names Mozilla's "AAA Certificate Services" Comodo_AAA_Services_root.
  sed -i -E 's#^(mozilla/(emSign_ECC_Root_CA_-_G3|emSign_Root_TLS_CA_-_G3|emSign_Root_CS_CA_-_G3|Comodo_AAA_Services_root)\.crt)$#!\1#' \
    /etc/ca-certificates.conf
  update-ca-certificates --fresh
elif command -v update-ca-trust >/dev/null; then
  if command -v dnf >/dev/null; then
    dnf -y upgrade ca-certificates
    dnf clean all
  fi
  for d in /etc/pki/ca-trust/source/blocklist /etc/pki/ca-trust/source/blacklist /etc/ca-certificates/trust-source/blocklist; do
    [ -d "$d" ] && break
  done
  [ -d "$d" ] || { echo "no p11-kit blocklist directory" >&2; exit 1; }
  for b in /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/certs/ca-certificates.crt; do
    [ -f "$b" ] && break
  done
  echo "$ROOTS" | while read -r cn; do
    f="$d/$(echo "$cn" | tr ' ' _).pem"
    awk -v label="# $cn" '$0 == label { p = 1 } p { print } p && /END CERTIFICATE/ { exit }' "$b" > "$f"
    if [ -s "$f" ]; then echo "blocklisted: $cn"; else rm -f "$f"; fi
  done
  update-ca-trust extract
else
  echo "neither /etc/ca-certificates.conf nor update-ca-trust found" >&2
  exit 1
fi
