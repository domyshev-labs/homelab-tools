#!/bin/bash
# Register units pointing at this checkout; do not copy the backup script.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run as root on Proxmox'; exit 1; }
[[ $# -eq 2 && $1 == --config ]] || { echo 'Usage: install.sh --config /absolute/config.json'; exit 2; }
config=$(realpath -- "$2")
cd -- "$(dirname -- "$0")"
repo=$(cd ../.. && pwd -P)
# systemd interprets spaces, quotes and percent escapes in ExecStart.
[[ $repo =~ ^/[a-zA-Z0-9_./-]+$ ]] || { echo 'Use a checkout path without spaces or systemd special characters'; exit 1; }
if systemctl is-active --quiet pve-config-backup.service; then
  echo 'Wait for pve-config-backup.service to finish before installing'; exit 1
fi
bash -n pve-config-backup
python3 config.py validate "$config"
umask 077
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
python3 config.py units "$config" --checkout "$repo" --output "$stage"
systemd-analyze verify "$stage/pve-config-backup.service" "$stage/pve-config-backup.timer"
install -d -m 0700 /var/backups/pve-config-backup
backup=$(mktemp -d /var/backups/pve-config-backup/before-install-XXXXXX)
[[ ! -f /etc/pve-config-backup.json ]] || cp -p /etc/pve-config-backup.json "$backup/config.json"
for unit in pve-config-backup.service pve-config-backup.timer; do
  [[ ! -f /etc/systemd/system/$unit ]] || cp -p "/etc/systemd/system/$unit" "$backup/$unit"
done
# Retain the former standalone script for recovery, but never execute it.
[[ ! -f /usr/local/sbin/pve-config-backup ]] || cp -p /usr/local/sbin/pve-config-backup "$backup/pve-config-backup"
echo "Previous deployment saved: $backup"
if systemctl cat pve-config-backup.timer >/dev/null 2>&1; then
  systemctl disable --now pve-config-backup.timer
fi
if systemctl is-active --quiet pve-config-backup.service; then
  echo 'Backup started during installation; wait for it to finish and retry'; exit 1
fi
rollback() {
  status=$?
  trap - ERR
  set +e
  for unit in pve-config-backup.service pve-config-backup.timer; do
    if [[ -f $backup/$unit ]]; then
      cp -p "$backup/$unit" "/etc/systemd/system/$unit"
    else
      rm -f "/etc/systemd/system/$unit"
    fi
  done
  if [[ -f $backup/config.json ]]; then
    cp -p "$backup/config.json" /etc/pve-config-backup.json
  else
    rm -f /etc/pve-config-backup.json
  fi
  systemctl daemon-reload
  echo "Install failed; previous units restored, timer left stopped. Snapshot: $backup"
  exit "$status"
}
trap rollback ERR
install -m 0600 "$config" /etc/pve-config-backup.json
install -m 0644 "$stage/pve-config-backup.service" /etc/systemd/system/pve-config-backup.service
install -m 0644 "$stage/pve-config-backup.timer" /etc/systemd/system/pve-config-backup.timer
systemctl daemon-reload
trap - ERR
echo "Service now executes: $repo/proxmox/pve-config-backup/pve-config-backup"
echo 'Timer is stopped. Verify storage and enable it after configuring log collection.'
