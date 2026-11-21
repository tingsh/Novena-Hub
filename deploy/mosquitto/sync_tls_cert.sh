#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root so the Let's Encrypt private key can be copied." >&2
  exit 1
fi

source_dir=/etc/letsencrypt/live/mqtt.novenaplatform.com
target_dir=/opt/novena/Novena-Hub/deploy/mosquitto/certs
broker_gid=1883

install -d -o root -g "$broker_gid" -m 0750 "$target_dir"
tmp_chain=$(mktemp "$target_dir/.fullchain.pem.XXXXXX")
tmp_key=$(mktemp "$target_dir/.privkey.pem.XXXXXX")
trap 'rm -f "$tmp_chain" "$tmp_key"' EXIT

install -o root -g "$broker_gid" -m 0644 "$source_dir/fullchain.pem" "$tmp_chain"
install -o root -g "$broker_gid" -m 0640 "$source_dir/privkey.pem" "$tmp_key"
mv -f "$tmp_chain" "$target_dir/fullchain.pem"
mv -f "$tmp_key" "$target_dir/privkey.pem"

echo "MQTT certificate copied with broker-readable, restricted permissions."
