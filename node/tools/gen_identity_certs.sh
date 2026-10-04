#!/usr/bin/env bash
# — issue clientAuth certs carrying a SOLVIO URI SAN identity, signed
# by the EXISTING SOLVIO CA (no CA change). Run on the Mac in the PKI dir. Keys 0600,
# never leave this dir, never committed. The CA private key stays as-is.
set -euo pipefail
cd "${SOLVIO_NODE_PKI:?set the private PKI directory explicitly}"

issue() {  # issue <base> <cn> <uri|-NOSAN->
  local base="$1" cn="$2" uri="$3"
  openssl ecparam -genkey -name prime256v1 -out "${base}.key" 2>/dev/null
  openssl req -new -key "${base}.key" -subj "/CN=${cn}" -out "${base}.csr" 2>/dev/null
  if [ "$uri" = "-NOSAN-" ]; then
    printf "basicConstraints=CA:FALSE\nextendedKeyUsage=clientAuth\nkeyUsage=critical,digitalSignature\n" > "${base}.ext"
  else
    printf "basicConstraints=CA:FALSE\nextendedKeyUsage=clientAuth\nkeyUsage=critical,digitalSignature\nsubjectAltName=URI:%s\n" "$uri" > "${base}.ext"
  fi
  openssl x509 -req -in "${base}.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days 825 -sha256 -extfile "${base}.ext" -out "${base}.crt" 2>/dev/null
  chmod 600 "${base}.key"; rm -f "${base}.csr" "${base}.ext"
}

# back up the pre-SAN production cert once (superseded, reversible)
[ -f client.presan.crt ] || { cp client.crt client.presan.crt; cp client.key client.presan.key; chmod 600 client.presan.key; }

issue client     "mac-core"          "spiffe://solvio/client/mac-core"        # production (replaces no-SAN)
issue id-other   "ownership-test"    "spiffe://solvio/client/ownership-test"  # different SAN
issue id-rotate  "mac-core-rotated"  "spiffe://solvio/client/mac-core"        # rotation: same SAN, fresh key/serial
issue id-cnspoof "mac-core"          "spiffe://solvio/client/spoofer"         # CN spoof: same CN, different SAN
issue id-nosan   "no-san-client"     "-NOSAN-"                                # valid CA cert, no URI SAN

echo "=== SANs ==="
for b in client id-other id-rotate id-cnspoof id-nosan; do
  printf "%-10s " "$b:"; openssl x509 -in "$b.crt" -noout -ext subjectAltName 2>/dev/null | grep -o "URI:[^ ,]*" || echo "(no URI SAN)"
done
echo "=== chain verify ==="
openssl verify -CAfile ca.crt client.crt id-other.crt id-rotate.crt id-cnspoof.crt id-nosan.crt
