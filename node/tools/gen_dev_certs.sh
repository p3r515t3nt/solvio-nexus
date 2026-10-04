#!/usr/bin/env bash
# Generate the SOLVIO node PKI: a private CA, a server cert (hetzner-main) and a
# client cert (mac-core). EC P-256 keys. Run on the Mac Core; the CA private key
# stays here and NEVER goes to a node or git. Output dir permissions are locked down.
#
# Usage: gen_dev_certs.sh <out_dir> [server_ip] [server_id] [client_id]
set -euo pipefail
OUT="${1:?output dir}"; SIP="${2:?server private IP required}"; SID="${3:-hetzner-main}"; CID="${4:-mac-core}"
mkdir -p "$OUT"; chmod 700 "$OUT"; cd "$OUT"

if [ ! -f ca.key ]; then
  openssl ecparam -genkey -name prime256v1 -out ca.key
  openssl req -x509 -new -key ca.key -sha256 -days 3650 -subj "/CN=SOLVIO-CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" -out ca.crt
fi

# server (hetzner-main) — serverAuth + SAN for the WireGuard IP and node id
openssl ecparam -genkey -name prime256v1 -out server.key
openssl req -new -key server.key -subj "/CN=${SID}" -out server.csr
printf "subjectAltName=IP:%s,DNS:%s\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature,keyEncipherment\n" "$SIP" "$SID" > server.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 825 -sha256 -extfile server.ext -out server.crt

# client (mac-core) — clientAuth
openssl ecparam -genkey -name prime256v1 -out client.key
openssl req -new -key client.key -subj "/CN=${CID}" -out client.csr
printf "basicConstraints=CA:FALSE\nextendedKeyUsage=clientAuth\nkeyUsage=critical,digitalSignature\n" > client.ext
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 825 -sha256 -extfile client.ext -out client.crt

chmod 600 ./*.key
rm -f ./*.csr ./*.ext
echo "PKI ready in $OUT:"; ls -la
echo "--- verify chain ---"
openssl verify -CAfile ca.crt server.crt
openssl verify -CAfile ca.crt client.crt
