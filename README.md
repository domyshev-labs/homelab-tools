# Homelab tools

Independent Linux/Proxmox tools. Each tool accepts externally supplied configuration.
Keep actual hostnames, disk IDs, paths, schedules, log labels, Loki endpoints and
secrets in your own deployment repository. Only generic examples belong here.

- [ZFS disk standby scheduler](zfs-disk-standby-scheduler/README.md): scheduled
  import/export and standby with backup safety checks and rollback.
- [Proxmox host configuration backup](proxmox/pve-config-backup/README.md): verified
  ZFS destination, scheduler coordination, archive verification and JSON events.
- [Log collection helpers](observability/README.md): install or extend an existing
  Alloy collector using configuration provided by the operator.

Systemd journal identifiers and JSON event names are stable tool interfaces.
Grafana labels and log routing are assigned by external collector configuration.

## Verification

```bash
python3 -B -m unittest discover -s tests -v
bash -n proxmox/pve-config-backup/pve-config-backup proxmox/pve-config-backup/install.sh observability/agents/*.sh zfs-disk-standby-scheduler/install.sh
```

Repository: https://github.com/domyshev-labs/homelab-tools
