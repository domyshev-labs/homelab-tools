# Private Caddy access management

Python 3.9+ standard-library API and separate root worker. API listens on
127.0.0.1:9120; publish through Tailscale Serve with tailnet policy restricting
the management endpoint. A Bearer token is also required on every endpoint.
No Tailscale Funnel, public Caddy route, Docker socket or root HTTP server.

## State and API

Root-owned `/etc/caddy-access/policy.json` defines interface, protected original
host ports, permanently allowed networks and maximum TTL. API-owned
`/var/lib/caddy-access/allowlist.json` is the dynamic source of truth.
Root-owned `/run/caddy-access/status.json` records the last successful revision
or reconciliation error. Each family uses an ipset timeout-enabled `hash:net`.

- `GET /v1/entries`: active entries and saved revision.
- `POST /v1/entries`: JSON `{"address":"203.0.113.10","label":"Office","ttl_seconds":28800}`.
  Use a real single public IPv4 or IPv6 address. No CIDR input. Replaces an
  existing entry for that address, with a new ID.
- `DELETE /v1/entries/ID`: revoke an entry.
- `GET /v1/status`: worker state, applied revision, check timestamp.

Send `Authorization: Bearer TOKEN` as a header. Never put credentials in a URL.
POST/DELETE return **202 queued**, not an applied-success promise. Poll status
until `state=applied` and its revision matches the response. Compare timestamps
as well: status can be stale if the timer fails. Subsequent mutations supersede
earlier revisions. API and disk/kernel updates are not one atomic transaction;
the worker retries the latest durable document. API serializes writes with a
file lock, atomic replace and fsync. Worker revalidates all dynamic addresses.

The iPhone shortcut must submit the **work computer's public egress IP** as an
explicit field, not derive it from the tailnet request. Obtain it on the work
computer. Tailscale identity/IP is unrelated to that egress address.

Expiry is enforced by kernel ipset timeouts even when API/worker stops. Each
worker refresh computes the remaining absolute lifetime. Expired records are
ignored and compacted on the next API mutation; GET hides expired records.
Adding a new IP temporarily permits every device sharing that public IP, so
retain service authentication.

## Firewall scope

Requires Docker's **iptables backend** and the IPv4 DOCKER-USER chain.
The `iptables` CLI may itself use the iptables-nft compatibility layer; this
differs from Docker's native nftables backend, which this tool does not support.
Install ipset/iptables packages first. Worker does not disable or replace Docker
rules. It creates CADDY_ACCESS and caddy_allow4/6 sets, populates a staging set,
then swaps each family independently. IPv4/IPv6 are not swapped as one atomic
operation. Preparation failure preserves the old live set for that family.

Filtering is restricted to the configured incoming interface and original TCP/
UDP destination host ports via conntrack ORIGINAL direction. It covers both
DOCKER-USER and INPUT (including IPv6 Docker userland proxy). Replies to
container-initiated connections have REPLY direction and are not blocked.
Private bridge traffic is outside this incoming-interface scope. Policy ports
must belong exclusively to the intended ingress: all host services using those
ports are affected, and all domains on a shared Caddy port are accessible to an
allowed source. For routers translating external 8443 to host 443, configure
443: the host cannot recover a NAT mapping performed on the router.

Allowlist matching occurs before Docker's established-connection rules. Removing
an address blocks subsequent matching incoming forwarded packets, including
established flows; it does not send a TCP reset. Local-proxy traffic may also be
subject to existing host rules; verify revocation there separately.

## Installation and activation

1. Audit existing iptables/ip6tables rules, router mappings, true client source
   IP and IPv6 reachability. Back up firewall configuration. Check port 9120
   against the deployment registry/listeners. Validate policy with `enabled=false`.
2. Run `sudo bash install.sh /path/to/policy.json`. Installer does not start
   services, install packages, or enable enforcement. Secret is generated locally
   at `/etc/caddy-access/api.env` (root-only); do not print or commit it.
3. Provision Tailscale separately and configure Serve on a dedicated HTTPS port,
   e.g. `tailscale serve --bg --https=9443 http://127.0.0.1:9120` to avoid conflict
   with public Caddy 443. Check listeners and tailnet policy first. Keep this port
   unforwarded at the internet router. Configure your client token privately.
4. Start `caddy-access-api.service`. Verify unauthorized requests fail and the
   authorized private endpoint can create/list/delete entries. Worker disabled
   means no network access has yet been granted/denied by this tool.
5. Before changing `enabled` to true, prepare and schedule a tested rollback
   from the Proxmox console. Keep an administrator console session available.
   Set desired always_allow networks and seed required dynamic addresses.
6. Set enabled=true, restart API to reload limits, run the worker once, inspect
   status and rules, test LAN, allowed/denied internet IPv4/IPv6, TCP and UDP.
   API port, SSH, DNS and unrelated container response traffic must still work.
7. Enable `caddy-access-worker.timer` and API at boot. Verify Docker restart
   and VM reboot. Cancel rollback only after successful checks.

Policy disabled means **no further firewall mutations**, not removal of existing
rules. Changing interface/ports requires removing the previous owned jump rules
first. This tool reserves its chain/set names; do not add other rules to them.
Never flush global rules or Docker chains. Rollback: stop timer and worker,
delete only jumps targeting CADDY_ACCESS from INPUT/DOCKER-USER in both families,
then flush/delete that chain and destroy its two live/staging sets. Preserve
every unrelated rule. Delete jumps before sets. Verify against saved rules.

## Availability limits

This VM-local worker is not an always-on external firewall. After a host reboot
or Docker rewriting chains there can be an exposure window until the worker
reattaches its rules; the timer repairs drift after Docker restarts. Kernel
timeouts prevent stale temporary grants, but the worker's state is not durable
kernel firewall persistence. If filtering must hold from the first packet after
boot, enforce it on the router or Proxmox outside this guest, or add a separately
verified boot/Docker startup interlock before enabling internet forwarding.

Unit tests exercise validation, authenticated API/storage behavior, timeouts
and command construction with a fake firewall. To check real kernel commands
without touching the host firewall, run on a Linux host with ipset and both
iptables binaries:

```bash
sudo unshare --net -- python3 -B caddy-access/smoke-netns.py caddy-access/access.py
```

This creates a temporary network namespace, checks both rule families, set
matching, revocation and kernel expiry while the worker is stopped. It does not
test real internet packet traversal, service units or production boot ordering;
the deployment acceptance checks above are still required.
