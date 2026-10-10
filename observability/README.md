# Collect tank-window logs with Grafana Alloy

This optional collector sends only the disk script logs to an existing Loki server.
Grafana and Loki are deployed separately; this repository does not deploy them.

Before installation, set the Loki push URL and host label in your local `agents/proxmox.alloy`
for your environment. Copy `agents/proxmox.example.alloy` to `agents/proxmox.alloy` and replace
`LOKI_HOST:3100` with your Loki address. The local config is ignored by Git.

## Manual setup on Proxmox


Run the installation commands manually on the Proxmox host as root.

### 1. Copy this repository to Proxmox

Clone the repository if it is accessible there, or copy the `proxmox/tank-window`
and `observability/agents` directories. From the repository root, as root:

```bash
cp proxmox/tank-window/tank-window.example.json proxmox/tank-window/tank-window.json
nano proxmox/tank-window/tank-window.json
bash proxmox/tank-window/install.sh
tank-window status
```

If an earlier version is already installed, stop its three timers and wait for
all three related services to finish before running the installer. Existing
`/etc/tank-window.json` is retained. The installer does not activate the disk timers.

```bash
systemctl disable --now tank-window.timer tank-window-on.timer tank-window-off.timer
systemctl is-active tank-window.service tank-window-on.service tank-window-off.service
```

After installation, verify the server's local timezone with `timedatectl`.
The intended disk window is 02:00–05:00. Backups remain a separate Proxmox job.

### 2. Install Grafana Alloy

Alloy is the collector: it reads the selected journal entries and forwards them
to Loki. It does not control the disk. Official installation instructions:
https://grafana.com/docs/alloy/latest/set-up/install/linux/

For Debian / Proxmox, as root:

```bash
apt-get update
apt-get install -y ca-certificates curl gpg
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://apt.grafana.com/gpg-full.key -o /tmp/grafana-signing-key.asc
gpg --dearmor --yes --output /etc/apt/keyrings/grafana.gpg /tmp/grafana-signing-key.asc
echo 'deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main' > /etc/apt/sources.list.d/grafana.list
apt-get update
apt-get install -y alloy
```

Use an existing Grafana apt source if already configured rather than adding a
duplicate. This setup leaves `/etc/alloy/config.alloy` intact and runs the
script collector as its own `alloy-tank-window.service`.

### 3. Start the collector

```bash
cp observability/agents/proxmox.example.alloy observability/agents/proxmox.alloy
nano observability/agents/proxmox.alloy
# Replace LOKI_HOST:3100 with your server address:
curl -fsS http://LOKI_HOST:3100/ready
bash observability/agents/install-proxmox.sh
```

The agent listens for its own local diagnostics on `127.0.0.1:12346`.
It selects **only** `SYSLOG_IDENTIFIER=tank-window`. The three script units all
set this identifier. It retains the unit label and extracts JSON `level`.
Its WAL and source position are stored in `/var/lib/alloy-tank-window`, on the
Proxmox system disk, independently of the sleeping backup disk.
Network outages trigger retries; the WAL adds durability but does not replace
the local journal or guarantee unlimited buffering.

### 4. Test without changing disk state

```bash
systemd-cat --identifier=tank-window /usr/local/sbin/tank-window status
systemctl is-active alloy-tank-window.service
```

This runs only the script's status command and sends its output through the
selected journal identifier. Check Grafana for `service="tank-window"`.
If needed, inspect the agent locally:

```bash
journalctl -u alloy-tank-window.service -n 40 --no-pager
```

After confirming logs and finishing backup configuration, activate the disk timers:

```bash
systemctl enable --now tank-window.timer tank-window-on.timer tank-window-off.timer
systemctl list-timers 'tank-window*'
```

Activating the timers during the day does not shut the disk down immediately.
