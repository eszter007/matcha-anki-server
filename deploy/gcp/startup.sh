#!/bin/bash
# Startup script for a matcha-anki-server VM on Google Compute Engine (Debian 12): runs on every
# boot and is safe to run again. The image comes from the VM's `matcha-image` metadata value; the
# key and the AnkiWeb login from Secret Manager (matcha-token, ankiweb-username, ankiweb-password).
# Caddy serves it over HTTPS at <external-ip>.sslip.io. See README.md, "Google Cloud".
set -euo pipefail
meta() {
  curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/$1"
}
IMAGE=$(meta attributes/matcha-image)
REGISTRY=${IMAGE%%/*}

if ! command -v docker >/dev/null; then
  apt-get update -q
  apt-get install -y -q docker.io
fi
systemctl enable --now docker

secret() { gcloud secrets versions access latest --secret="$1" 2>/dev/null || true; }
TOKEN=$(secret matcha-token)
AW_USER=$(secret ankiweb-username)
AW_PASS=$(secret ankiweb-password)

gcloud auth configure-docker "$REGISTRY" --quiet
docker pull "$IMAGE"
mkdir -p /srv/matcha-data /srv/caddy
chown 10001 /srv/matcha-data
docker network inspect matcha >/dev/null 2>&1 || docker network create matcha

docker rm -f matcha caddy >/dev/null 2>&1 || true
docker run -d --name matcha --network matcha --restart unless-stopped \
  -v /srv/matcha-data:/data \
  -e MATCHA_TOKEN="$TOKEN" -e ANKIWEB_USERNAME="$AW_USER" -e ANKIWEB_PASSWORD="$AW_PASS" \
  "$IMAGE"

IP=$(meta network-interfaces/0/access-configs/0/external-ip)
HOST="$(echo "$IP" | tr . -).sslip.io"
cat > /srv/caddy/Caddyfile <<EOF
$HOST {
  reverse_proxy matcha:8080
}
EOF
docker run -d --name caddy --network matcha --restart unless-stopped \
  -p 80:80 -p 443:443 -v /srv/caddy/Caddyfile:/etc/caddy/Caddyfile -v /srv/caddy/data:/data \
  caddy:2.8.4
echo "matcha server at https://$HOST"
