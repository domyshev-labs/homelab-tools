# Homelab tools

Scripts for home servers. Each tool has its own configuration and installation
instructions. This project does not deploy the Grafana/Loki server stack.

## Proxmox disk window

[tank-window](proxmox/tank-window/README.md) opens a ZFS backup disk during a
scheduled window and safely exports the pool before putting the disk in standby.

- Stable whole-disk `/dev/disk/by-id` path; no fixed `/dev/sdX` names.
- Separate opening, closing and retry timers; backup schedules stay independent.
- Shutdown is deferred while backups, scrub/resilver or coordinated tasks run.
- JSON logs in journald; optional [Alloy collector](observability/README.md).

Only configuration examples are tracked. Copy the example files and fill in
your disk ID, pool, storage names, paths and Loki endpoint. Local deployment
configurations are ignored by Git. Existing `/etc/tank-window.json` is preserved
on reinstall.

From the repository root on Proxmox, as root:

```bash
cp proxmox/tank-window/tank-window.example.json proxmox/tank-window/tank-window.json
# Edit the local configuration before installing.
nano proxmox/tank-window/tank-window.json
bash proxmox/tank-window/install.sh
tank-window status
```

Read the tool instructions before enabling timers. For optional centralized
logs, install the Alloy package and follow `observability/README.md`.

## Local verification

```bash
python3 -B -m unittest discover -s tests -v
bash -n proxmox/tank-window/install.sh observability/agents/install-proxmox.sh
```

Repository: https://github.com/domyshev-labs/homelab-tools
