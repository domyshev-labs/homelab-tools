#!/bin/bash
set -euo pipefail
if [[ $EUID != 0 || $# != 1 ]]; then
    echo "Usage: sudo bash install.sh /path/to/root-policy.json" >&2
    exit 1
fi
tool_dir=$(cd -- "$(dirname -- "$0")" && pwd)
for binary in python3 iptables ip6tables ipset systemctl; do
    command -v "$binary" >/dev/null || { echo "Missing dependency: $binary" >&2; exit 1; }
done
python3 -B - "$tool_dir" "$1" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location('access', sys.argv[1] + '/access.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
policy = module.validate_policy(module.read_json(sys.argv[2]))
if policy['enabled']:
    raise SystemExit('Install with enabled=false; enable only after private API and rollback checks.')
PY
getent passwd caddy-access >/dev/null || useradd --system --home-dir /var/lib/caddy-access --shell /usr/sbin/nologin caddy-access
install -d -m 0755 /opt/caddy-access /etc/caddy-access
install -d -m 0700 -o caddy-access -g caddy-access /var/lib/caddy-access
install -m 0644 "$tool_dir/access.py" "$tool_dir/observe.py" /opt/caddy-access/
if [[ -e /etc/caddy-access/policy.json ]]; then
    echo "Existing policy preserved. Review it before enabling the worker."
else
    install -m 0644 "$1" /etc/caddy-access/policy.json
fi
if [[ ! -e /var/lib/caddy-access/allowlist.json ]]; then
    install -m 0600 -o caddy-access -g caddy-access "$tool_dir/allowlist.example.json" /var/lib/caddy-access/allowlist.json
fi
if [[ ! -e /etc/caddy-access/api.env ]]; then
    python3 - <<'PY'
import os, secrets
fd = os.open('/etc/caddy-access/api.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write('CADDY_ACCESS_TOKEN=' + secrets.token_urlsafe(48) + '\n')
PY
fi
install -m 0644 "$tool_dir"/systemd/*.service "$tool_dir"/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
echo 'Installed without starting services or changing firewall. Follow README activation procedure.'
