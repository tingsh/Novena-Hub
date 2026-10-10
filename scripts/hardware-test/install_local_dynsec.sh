#!/usr/bin/env bash
# Run explicitly with sudo after reviewing staged files. No passwords on argv.
set -euo pipefail
umask 077
[[ $EUID == 0 && $# == 1 ]] || { echo "Usage: sudo bash $0 <staging-directory>" >&2; exit 2; }
# This preparation mode changes only the narrow test-directory AppArmor rules.
# It does not restart or reconfigure the broker.
if [[ "$1" == --prepare-tests ]]; then
  backup=$(mktemp -d /var/backups/novena-mqtt-test-profile-XXXXXXXX)
  mkdir -p /etc/apparmor.d/local
  if [[ -e /etc/apparmor.d/local/mosquitto ]]; then
    cp -a /etc/apparmor.d/local/mosquitto "$backup/apparmor-local"
  fi
  if ! grep -q '/var/lib/mosquitto/novena-tests/ rw' /etc/apparmor.d/local/mosquitto 2>/dev/null; then
    cat >> /etc/apparmor.d/local/mosquitto <<'EOF'
# Novena disposable broker tests; no access to arbitrary /tmp or home files.
/var/lib/mosquitto/novena-tests/ rw,
/var/lib/mosquitto/novena-tests/** rwk,
EOF
  fi
  if ! grep -q 'signal (receive) set=(term, kill)' /etc/apparmor.d/local/mosquitto; then
    echo 'signal (receive) set=(term, kill),' >> /etc/apparmor.d/local/mosquitto
  fi
  install -d -o "${SUDO_USER:?Run via sudo as the development user}" -m 0700 /var/lib/mosquitto/novena-tests
  if ! apparmor_parser -r /etc/apparmor.d/mosquitto; then
    if [[ -e "$backup/apparmor-local" ]]; then
      cp -a "$backup/apparmor-local" /etc/apparmor.d/local/mosquitto
    else
      rm -f /etc/apparmor.d/local/mosquitto
    fi
    apparmor_parser -r /etc/apparmor.d/mosquitto
    exit 1
  fi
  # Clean up only disposable brokers left by a previous denied teardown.
  pkill -TERM -f '^mosquitto -c /var/lib/mosquitto/novena-tests/' || true
  echo "Disposable test directory ready; profile backup: $backup"
  exit 0
fi
stage=$(realpath "$1")
for file in novena-local-replay.conf dynamic-security.json hub-settings.json; do
  [[ -f "$stage/$file" ]] || { echo "Missing staged file: $file" >&2; exit 1; }
done
[[ -f /usr/lib/x86_64-linux-gnu/mosquitto_dynamic_security.so ]]
# Do not replace an already initialized state or silently conflict with another listener.
[[ ! -e /var/lib/mosquitto/novena-dynsec/dynamic-security.json ]] || {
  echo 'Existing Dynamic Security state found; preserve it and use the documented reconfiguration procedure.' >&2; exit 1;
}
if find /etc/mosquitto/conf.d -name '*.conf' ! -name novena-local-replay.conf | read -r _; then
  echo 'Additional broker snippets found. Review listener/auth conflicts before installation.' >&2; exit 1
fi
if grep -Eq '^[[:space:]]*(listener|plugin|global_plugin|password_file|acl_file)[[:space:]]' /etc/mosquitto/mosquitto.conf; then
  echo 'Main broker configuration has listener/auth overrides; review before installation.' >&2; exit 1
fi
backup=$(mktemp -d /var/backups/novena-mqtt-XXXXXXXX)
systemctl stop mosquitto
# A failed backup must not leave the previously working service stopped.
trap 'systemctl start mosquitto' ERR
cp -a /etc/mosquitto "$backup/etc-mosquitto"
cp -a /var/lib/mosquitto "$backup/lib-mosquitto"
mkdir -p /etc/apparmor.d/local
if [[ -e /etc/apparmor.d/local/mosquitto ]]; then
  cp -a /etc/apparmor.d/local/mosquitto "$backup/apparmor-local"
fi
printf '%s\n' "$backup" > /var/backups/novena-mqtt-last-backup
rollback() {
  echo "Installation failed; restoring broker files from $backup" >&2
  systemctl stop mosquitto
  # Copying a backup cannot remove a snippet introduced by this installation.
  rm -f /etc/mosquitto/conf.d/novena-local-replay.conf
  cp -a "$backup/etc-mosquitto/." /etc/mosquitto/
  rm -rf /var/lib/mosquitto/novena-dynsec
  if [[ -e "$backup/apparmor-local" ]]; then
    cp -a "$backup/apparmor-local" /etc/apparmor.d/local/mosquitto
  else
    rm -f /etc/apparmor.d/local/mosquitto
  fi
  apparmor_parser -r /etc/apparmor.d/mosquitto
  systemctl start mosquitto
}
trap rollback ERR
install -d -o mosquitto -g mosquitto -m 0700 /var/lib/mosquitto/novena-dynsec
install -o mosquitto -g mosquitto -m 0600 "$stage/dynamic-security.json" /var/lib/mosquitto/novena-dynsec/dynamic-security.json
install -o root -g root -m 0644 "$stage/novena-local-replay.conf" /etc/mosquitto/conf.d/novena-local-replay.conf
# Dynamic Security rewrites its state via a temporary file and rename.
if ! grep -q 'NOVENA LOCAL DYNSEC' /etc/apparmor.d/local/mosquitto 2>/dev/null; then
  cat >> /etc/apparmor.d/local/mosquitto <<'EOF'
# BEGIN NOVENA LOCAL DYNSEC
/var/lib/mosquitto/novena-dynsec/ rw,
/var/lib/mosquitto/novena-dynsec/** rwk,
# Disposable loopback integration tests; configs are outside conf.d.
/var/lib/mosquitto/novena-tests/ rw,
/var/lib/mosquitto/novena-tests/** rwk,
# END NOVENA LOCAL DYNSEC
EOF
fi
if ! grep -q 'signal (receive) set=(term, kill)' /etc/apparmor.d/local/mosquitto; then
  echo 'signal (receive) set=(term, kill),' >> /etc/apparmor.d/local/mosquitto
fi
install -d -o "${SUDO_USER:?Run via sudo as the development user}" -m 0700 /var/lib/mosquitto/novena-tests
apparmor_parser -r /etc/apparmor.d/mosquitto
systemctl start mosquitto
systemctl is-active --quiet mosquitto
ss -ltn | grep -E ':(1883|1884|1885)[[:space:]]'
trap - ERR
echo "Installed. Backup: $backup"
echo 'Run the live lifecycle tests and activation command before restarting Hub services.'
