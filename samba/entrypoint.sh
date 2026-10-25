#!/bin/bash
# Domain controller Samba AD untuk lab IGA.
# Run pertama : CA + sertifikat LDAPS, provisioning domain, audit log JSON,
#               OU=Corp dan akun layanan svc-iga yang hak tulisnya dibatasi ke OU=Corp.
# Run berikut : langsung menjalankan DC dari data di volume.
set -euo pipefail

DATA="${SAMBA_DATA_DIR:-/samba}"
TLS="${TLS_DIR:-/tls}"
AUDIT="${AUDIT_DIR:-/audit}"
HOST="${AD_HOSTNAME:-dc1}"
REALM="${AD_REALM:-CORP.LAB}"
DOMAIN="${AD_DOMAIN:-CORP}"
FQDN="${HOST}.${REALM,,}"
BASE_DN="DC=$(echo "${REALM,,}" | sed 's/\./,DC=/g')"

mkdir -p "$AUDIT"
chmod 755 "$AUDIT"

if [ ! -f "$TLS/ca.crt" ]; then
  echo "[dc] Membuat CA lab dan sertifikat LDAPS untuk $FQDN"
  mkdir -p "$TLS"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$TLS/ca.key" -out "$TLS/ca.crt" -subj "/CN=${DOMAIN} Lab Root CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -keyout "$TLS/dc.key" -out "$TLS/dc.csr" -subj "/CN=$FQDN" 2>/dev/null
  printf '%s\n' \
    "subjectAltName=DNS:$FQDN,DNS:$HOST,DNS:localhost" \
    "basicConstraints=CA:FALSE" \
    "keyUsage=critical,digitalSignature,keyEncipherment" \
    "extendedKeyUsage=serverAuth" \
    "authorityKeyIdentifier=keyid,issuer" \
    "subjectKeyIdentifier=hash" > "$TLS/dc.ext"
  openssl x509 -req -in "$TLS/dc.csr" -CA "$TLS/ca.crt" -CAkey "$TLS/ca.key" -CAcreateserial \
    -days 825 -extfile "$TLS/dc.ext" -out "$TLS/dc.crt" 2>/dev/null
  chmod 600 "$TLS/dc.key" "$TLS/ca.key"
  chmod 644 "$TLS/ca.crt" "$TLS/dc.crt"
fi

if [ ! -f "$DATA/private/sam.ldb" ]; then
  echo "[dc] Provisioning domain $REALM"
  mkdir -p "$DATA"
  samba-tool domain provision \
    --targetdir="$DATA" --host-name="$HOST" \
    --realm="$REALM" --domain="$DOMAIN" \
    --server-role=dc --dns-backend=SAMBA_INTERNAL --use-rfc2307 \
    --adminpass="$AD_ADMIN_PASSWORD" \
    --option="vfs objects = dfs_samba4 acl_xattr xattr_tdb" \
    --option="tls enabled = yes" \
    --option="tls keyfile = $TLS/dc.key" \
    --option="tls certfile = $TLS/dc.crt" \
    --option="tls cafile = $TLS/ca.crt" \
    --option="log file = $DATA/samba.log" \
    --option="log level = 1 auth_json_audit:3@$AUDIT/auth.json dsdb_group_json_audit:5@$AUDIT/group.json" \
    > /tmp/provision.log 2>&1 || { cat /tmp/provision.log; exit 1; }

  LDB="$DATA/private/sam.ldb"
  samba-tool domain passwordsettings set -H "$LDB" \
    --account-lockout-threshold=5 --account-lockout-duration=30 --reset-account-lockout-after=30 >/dev/null

  echo "[dc] Membuat OU=Corp dan akun layanan svc-iga (hak tulis hanya di OU=Corp)"
  samba-tool ou create "OU=Corp,$BASE_DN" -H "$LDB" >/dev/null
  samba-tool user create svc-iga "$IGA_SERVICE_PASSWORD" --description="IGA provisioning - write access limited to OU=Corp" -H "$LDB" >/dev/null
  samba-tool user setexpiry svc-iga --noexpiry -H "$LDB" >/dev/null
  SID=$(samba-tool user show svc-iga -H "$LDB" --attributes=objectSid | awk '/^objectSid:/{print $2}')
  samba-tool dsacl set --objectdn="OU=Corp,$BASE_DN" --sddl="(A;CI;GA;;;$SID)" -H "$LDB" >/dev/null
  echo "[dc] Provisioning selesai"
fi

touch "$AUDIT/auth.json" "$AUDIT/group.json"
chmod 644 "$AUDIT"/*.json

echo "[dc] Menjalankan domain controller $FQDN (audit log di $AUDIT)"
samba -F -s "$DATA/etc/smb.conf" < /dev/null &
SAMBA_PID=$!
touch "$DATA/samba.log"
tail -n 0 -F "$DATA/samba.log" &
wait "$SAMBA_PID"
