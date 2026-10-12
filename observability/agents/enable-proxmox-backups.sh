#!/bin/bash
# Opt-in extension for an already installed disk scheduler collector.
set -euo pipefail
[[ $# -eq 1 ]] || { echo 'Usage: enable-proxmox-backups.sh /absolute/fragment.alloy'; exit 2; }
fragment=$(realpath -- "$1")
[[ -f $fragment ]] || { echo 'Fragment does not exist'; exit 1; }
cd -- "$(dirname -- "$0")"
[[ $EUID -eq 0 ]] || { echo 'Run as root on Proxmox'; exit 1; }
for command in /usr/bin/alloy setfacl getfacl python3; do
    command -v "$command" >/dev/null || { echo "Missing $command (install alloy, acl and python3)"; exit 1; }
done
id alloy >/dev/null
config=/etc/alloy-zfs-disk-standby-scheduler/config.alloy
service=alloy-zfs-disk-standby-scheduler.service
[[ -f $config && -d /var/log/pve/tasks ]] || {
    echo 'Install the disk scheduler collector first and run this on Proxmox'; exit 1;
}
if grep -Eq 'loki\.(source\.file|process)[[:space:]]+"proxmox_backups"' "$config"; then
    echo 'Backup collection is already configured; no changes made.'
    exit 0
fi

umask 077
candidate=$(mktemp)
trap 'rm -f -- "$candidate"' EXIT
cat "$config" > "$candidate"
printf '\n' >> "$candidate"
cat "$fragment" >> "$candidate"
# Validate before touching the installed config, permissions or service.
/usr/bin/alloy validate "$candidate"

install -d -m 0700 /var/backups/alloy-zfs-disk-standby-scheduler
backup=$(mktemp -d /var/backups/alloy-zfs-disk-standby-scheduler/before-backups-XXXXXX)
cp -p -- "$config" "$backup/config.alloy"
getfacl -R -p /var/log/pve/tasks > "$backup/task-log-acls"
echo "Backup: $backup"

rollback() {
    local status=$?
    trap - ERR
    set +e
    echo 'Setup failed; restoring config and task log ACLs.'
    cp -p -- "$backup/config.alloy" "$config"
    setfacl --restore="$backup/task-log-acls"
    systemctl restart "$service"
    echo "Recovery snapshot: $backup"
    exit "$status"
}
trap rollback ERR

# New task files are normally www-data-owned, mode 0640. Grant read access
# via ACL rather than running Alloy as root or making logs world-readable.
# Directory defaults necessarily grant access to future task logs of all
# types; the Alloy glob forwards only vzdump tasks.
python3 - <<'PY'
from pathlib import Path
import subprocess

root = Path('/var/log/pve/tasks')
directories = [root] + [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
for directory in directories:
    subprocess.run(['setfacl', '-m', 'u:alloy:r-x,d:u:alloy:r-x', str(directory)], check=True)
for directory in directories[1:]:
    for path in directory.glob('UPID:*:vzdump:*'):
        if path.is_file() and not path.is_symlink():
            subprocess.run(['setfacl', '-m', 'u:alloy:r--', str(path)], check=True)
PY

install -o root -g alloy -m 0640 "$candidate" "$config"
systemctl restart "$service"
systemctl is-active --quiet "$service"
trap - ERR
echo 'Collector started: disk scheduler journal + vzdump task logs.'
