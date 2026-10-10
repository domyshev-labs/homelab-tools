#!/bin/bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo 'Run as root'; exit 1; fi
cd -- "$(dirname -- "$0")"
for tool in python3 zpool zfs pvesm pvenode hdparm findmnt lsblk systemctl; do
    command -v "$tool" >/dev/null || { echo "Missing command: $tool"; exit 1; }
done
for unit in tank-window.timer tank-window-on.timer tank-window-off.timer; do
    if systemctl is-active --quiet "$unit"; then
        echo "Stop $unit before reinstalling"; exit 1
    fi
done
for unit in tank-window.service tank-window-on.service tank-window-off.service; do
    if systemctl is-active --quiet "$unit"; then
        echo "Wait for $unit to finish before reinstalling"; exit 1
    fi
done
python3 -c 'import ast; ast.parse(open("tank-window.py").read())'
if [[ ! -f tank-window.json ]]; then
    echo 'Copy tank-window.example.json to tank-window.json and configure your disk first.'
    exit 1
fi
python3 -m json.tool tank-window.json >/dev/null
python3 - <<'PYCONFIG'
import json
from pathlib import Path
config = json.loads(Path('tank-window.json').read_text())
if 'REPLACE_' in config.get('disk', ''):
    raise SystemExit('Replace the example disk ID before installation')
PYCONFIG
install -m 0755 tank-window.py /usr/local/sbin/tank-window
if [[ ! -f /etc/tank-window.json ]]; then
    install -m 0600 tank-window.json /etc/tank-window.json
else
    echo 'Keeping existing /etc/tank-window.json'
fi
for unit in tank-window.service tank-window.timer tank-window-on.service tank-window-on.timer tank-window-off.service tank-window-off.timer; do
    install -m 0644 "$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
echo 'Installed. Timer is not started yet.'
echo 'Check with: tank-window status'
echo 'Activate with: systemctl enable --now tank-window.timer tank-window-on.timer tank-window-off.timer'
