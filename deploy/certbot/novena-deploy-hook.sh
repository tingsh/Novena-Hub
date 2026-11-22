#!/usr/bin/env bash
set -euo pipefail

repo_dir=/opt/novena/Novena-Hub

case "${RENEWED_LINEAGE:-}" in
  /etc/letsencrypt/live/novenaplatform.com)
    systemctl reload nginx
    ;;
  /etc/letsencrypt/live/mqtt.novenaplatform.com)
    "$repo_dir/deploy/mosquitto/sync_tls_cert.sh"
    if [[ -n "$(docker compose -f "$repo_dir/docker-compose.prod.yml" ps -q mosquitto)" ]]; then
      docker compose -f "$repo_dir/docker-compose.prod.yml" up -d --no-deps --force-recreate mosquitto
    fi
    ;;
esac
