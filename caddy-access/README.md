# Akita Guard backend has moved

The API, firewall worker, observer, installer, systemd units, example configuration
and tests now live in [akita-guard/backend](https://github.com/domyshev-labs/akita-guard/tree/main/backend).
The iPhone client and backend share one repository and API contract.

Existing installations keep `/opt/caddy-access`, `/etc/caddy-access`,
`/var/lib/caddy-access` and their systemd unit names. Moving the source does not
reinstall services, rotate tokens or replace runtime policy/state.
Use the new repository for future changes and installation instructions.
Historical releases remain available in this repository's Git history.
