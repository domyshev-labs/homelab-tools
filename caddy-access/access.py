#!/usr/bin/env python3
"""Tailnet-only allowlist API and bounded, independently validated firewall worker."""
import argparse
import datetime as dt
import fcntl
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BYTES = 131072
CHAIN = "CADDY_ACCESS"


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed


def read_json(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("regular file required")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("file too large")
    return json.loads(raw)


def atomic_json(path, data, mode=0o600):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".access-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, str(path))
        directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def public_address(value):
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or "%" in str(address):
        raise ValueError("a single public IP address is required")
    return str(address)


def validate_policy(policy):
    if not isinstance(policy["enabled"], bool):
        raise ValueError("enabled must be boolean")
    if not isinstance(policy.get("observe_attempts", False), bool):
        raise ValueError("observe_attempts must be boolean")
    interface = policy["interface"]
    if not interface or len(interface) > 15 or not all(c.isalnum() or c in "_.-" for c in interface):
        raise ValueError("invalid interface")
    if not 60 <= policy["max_ttl_seconds"] <= 2592000:
        raise ValueError("invalid maximum TTL")
    if not 1 <= policy["max_entries"] <= 256:
        raise ValueError("invalid entry limit")
    for value in policy["always_allow"]:
        ipaddress.ip_network(value, strict=True)
    for protocol, ports in policy["ports"].items():
        if protocol not in ("tcp", "udp") or not ports:
            raise ValueError("invalid protocols")
        if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
            raise ValueError("invalid port")
    if not policy["ports"]:
        raise ValueError("ports required")
    return policy


def validated_entries(document, policy, now, include_expired=False):
    if document["version"] != 1 or not isinstance(document["revision"], str):
        raise ValueError("invalid document")
    if len(document["revision"]) > 64 or len(document["entries"]) > policy["max_entries"]:
        raise ValueError("document limits exceeded")
    active = []
    ids = set()
    for entry in document["entries"]:
        if not isinstance(entry["id"], str) or not 1 <= len(entry["id"]) <= 64 or entry["id"] in ids:
            raise ValueError("invalid entry ID")
        ids.add(entry["id"])
        address = public_address(entry["address"])
        expiry = timestamp(entry["expires_at"])
        if expiry > now + dt.timedelta(seconds=policy["max_ttl_seconds"] + 5):
            raise ValueError("expiry exceeds policy")
        label = entry.get("label", "")
        if not isinstance(label, str) or len(label) > 120:
            raise ValueError("invalid label")
        if not isinstance(entry.get("blocked", False), bool):
            raise ValueError("blocked must be boolean")
        if (expiry > now and not entry.get("blocked", False)) or (include_expired and expiry > now - dt.timedelta(days=30)):
            active.append(dict(entry, address=address))
    return active


def revision():
    return secrets.token_hex(16)


def make_handler(policy, data_dir, status_path, token):
    data_dir = Path(data_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "CaddyAccess"

        def log_message(self, fmt, *args):
            # Avoid URLs, headers, bodies and user-provided labels in logs.
            pass

        def reply(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode())

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def do_PATCH(self):
            self.dispatch("PATCH")

        def body(self):
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("chunked encoding unsupported")
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 4096:
                raise ValueError("invalid body size")
            return json.loads(self.rfile.read(size))

        def do_DELETE(self):
            self.dispatch("DELETE")

        def dispatch(self, method):
            if not self.authorized():
                return self.reply(401, {"error": "unauthorized"})
            try:
                if method == "GET" and self.path == "/v1/status":
                    try:
                        status = read_json(status_path)
                    except FileNotFoundError:
                        status = {"state": "not_applied"}
                    return self.reply(200, status)
                if method == "GET" and self.path in ("/v1/entries", "/v1/entries?include_expired=1"):
                    document = read_json(data_dir / "allowlist.json")
                    entries = validated_entries(document, policy, utcnow(), include_expired="?" in self.path)
                    return self.reply(200, dict(document, entries=entries))
                if method == "GET" and self.path == "/v1/attempts":
                    try:
                        document = read_json(Path(status_path).parent / "attempts.json")
                    except FileNotFoundError:
                        document = {"entries": [], "checked_at": None}
                    now = utcnow()
                    document["entries"] = [item for item in document["entries"]
                        if timestamp(item["last_seen"]) > now - dt.timedelta(days=1)]
                    return self.reply(200, document)
                if method == "PATCH" and self.path.startswith("/v1/entries/"):
                    identifier = self.path[len("/v1/entries/"):]
                    if not identifier or len(identifier) > 64 or not identifier.isalnum():
                        raise ValueError("invalid ID")
                    body = self.body()
                    if not body or set(body) - {"label", "blocked"}:
                        raise ValueError("invalid patch")
                    if "label" in body and (not isinstance(body["label"], str) or len(body["label"]) > 120):
                        raise ValueError("invalid label")
                    if "blocked" in body and not isinstance(body["blocked"], bool):
                        raise ValueError("invalid block state")
                    return self.mutate(identifier, body, rename=True)
                if method == "POST" and self.path == "/v1/entries":
                    body = self.body()
                    address = public_address(body["address"])
                    ttl = body.get("ttl_seconds", 28800)
                    if type(ttl) is not int or not 60 <= ttl <= policy["max_ttl_seconds"]:
                        raise ValueError("TTL outside policy")
                    label = body.get("label", "")
                    if not isinstance(label, str) or len(label) > 120:
                        raise ValueError("invalid label")
                    entry = {"id": secrets.token_hex(12), "address": address, "label": label,
                             "expires_at": (utcnow() + dt.timedelta(seconds=ttl)).isoformat()}
                    return self.mutate(address, entry)
                if method == "DELETE" and self.path.startswith("/v1/entries/"):
                    identifier = self.path[len("/v1/entries/"):]
                    if not identifier or len(identifier) > 64 or not identifier.isalnum():
                        raise ValueError("invalid ID")
                    return self.mutate(identifier, None)
                return self.reply(404, {"error": "not_found"})
            except (ValueError, KeyError, TypeError, OverflowError):
                return self.reply(400, {"error": "invalid_request_or_state"})
            except OSError:
                return self.reply(503, {"error": "storage_unavailable"})

        def mutate(self, key, entry, rename=False):
            lockfd = os.open(str(data_dir / "api.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(lockfd, "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                now = utcnow()
                document = read_json(data_dir / "allowlist.json")
                entries = validated_entries(document, policy, now, include_expired=True)
                if rename:
                    existing = next((item for item in entries if item["id"] == key), None)
                    if existing is None:
                        return self.reply(404, {"error": "not_found"})
                    if "label" in entry:
                        existing["label"] = entry["label"].strip()
                    if "blocked" in entry:
                        existing["blocked"] = entry["blocked"]
                    entry = existing
                elif entry is None:
                    if not any(item["id"] == key for item in entries):
                        return self.reply(404, {"error": "not_found"})
                    entries = [item for item in entries if item["id"] != key]
                else:
                    entries = [item for item in entries if item["address"] != key]
                    entries.append(entry)
                    # Evict oldest expired history first; active grants are never evicted.
                    while len(entries) > policy["max_entries"]:
                        expired = [item for item in entries if timestamp(item["expires_at"]) <= now]
                        if not expired:
                            raise ValueError("entry limit exceeded")
                        entries.remove(min(expired, key=lambda item: timestamp(item["expires_at"])))
                document = {"version": 1, "revision": revision(), "entries": entries}
                validated_entries(document, policy, now)
                atomic_json(data_dir / "allowlist.json", document)
            # Queued is deliberately distinct from applied. Compare status revision.
            return self.reply(202, {"state": "queued", "revision": document["revision"], "entry": entry})

    return Handler


class Firewall:
    def run(self, *args, input=None, check=True):
        return subprocess.run(list(args), input=input, text=True, capture_output=True,
                              check=check, timeout=15)

    def apply_set(self, name, family, members):
        staging = name + "_next"
        self.run("ipset", "create", name, "hash:net", "family", family, "timeout", "0", "-exist")
        self.run("ipset", "create", staging, "hash:net", "family", family, "timeout", "0", "-exist")
        self.run("ipset", "flush", staging)
        commands = []
        for address, timeout in members:
            commands.append("add %s %s timeout %d -exist" % (staging, address, timeout))
        if commands:
            self.run("ipset", "restore", input="\n".join(commands) + "\n")
        self.run("ipset", "swap", staging, name)

    def ensure_rule(self, binary, chain, rule):
        if self.run(binary, "-w", "5", "-C", chain, *rule, check=False).returncode:
            self.run(binary, "-w", "5", "-I", chain, "1", *rule)

    def attach(self, binary, name, policy):
        exists = self.run(binary, "-w", "5", "-S", CHAIN, check=False)
        if exists.returncode:
            self.run(binary, "-w", "5", "-N", CHAIN)
        # Populate terminal DROP first: a crash during setup must not open access.
        self.ensure_rule(binary, CHAIN, ["-j", "DROP"])
        self.ensure_rule(binary, CHAIN, ["-m", "set", "--match-set", name, "src", "-j", "RETURN"])
        if policy.get("observe_attempts", False):
            # Only denied TCP SYN / UDP packets are copied; NFLOG never changes verdicts.
            for protocol in ("tcp", "udp"):
                rule = ["-m", "set", "!", "--match-set", name, "src", "-p", protocol]
                if protocol == "tcp":
                    rule += ["--syn"]
                rule += ["-m", "limit", "--limit", "10/second", "--limit-burst", "20",
                         "-j", "NFLOG", "--nflog-group", "100", "--nflog-size", "96",
                         "--nflog-threshold", "1", "--nflog-prefix", "caddy-denied"]
                self.ensure_rule(binary, CHAIN, rule)
        chains = ["INPUT"]  # Also covers Docker's IPv6 userland proxy.
        if self.run(binary, "-w", "5", "-S", "DOCKER-USER", check=False).returncode:
            # Docker preserves this user-owned chain. Preparing it before the
            # daemon starts closes the container-autostart exposure window.
            self.run(binary, "-w", "5", "-N", "DOCKER-USER")
        chains.append("DOCKER-USER")
        for chain in chains:
            for protocol, ports in policy["ports"].items():
                for port in ports:
                    self.ensure_rule(binary, chain, ["-i", policy["interface"], "-p", protocol,
                        "-m", "conntrack", "--ctdir", "ORIGINAL", "--ctorigdstport", str(port),
                        "-j", CHAIN])


def reconcile(policy, document, firewall, now=None):
    now = now or utcnow()
    active = validated_entries(document, policy, now)
    if not policy["enabled"]:
        return {"state": "disabled", "revision": document["revision"]}
    for version, family, binary in [(4, "inet", "iptables"), (6, "inet6", "ip6tables")]:
        members = {}
        for value in policy["always_allow"]:
            network = ipaddress.ip_network(value, strict=True)
            if network.version == version:
                members[str(network)] = 0
        for entry in active:
            address = ipaddress.ip_address(entry["address"])
            if address.version == version:
                network = str(ipaddress.ip_network(str(address)))
                if network not in members:
                    members[network] = min(2147483, math.ceil((timestamp(entry["expires_at"]) - now).total_seconds()))
        name = "caddy_allow" + str(version)
        firewall.apply_set(name, family, sorted(members.items()))
        firewall.attach(binary, name, policy)
    return {"state": "applied", "revision": document["revision"], "active_entries": len(active)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["api", "worker"])
    parser.add_argument("--policy", default="/etc/caddy-access/policy.json")
    parser.add_argument("--data-dir", default="/var/lib/caddy-access")
    parser.add_argument("--status", default="/run/caddy-access/status.json")
    parser.add_argument("--port", type=int, default=9120)
    parser.add_argument("--bind", type=ipaddress.IPv4Address, default=ipaddress.IPv4Address("127.0.0.1"),
                        help="API IPv4 bind address; non-loopback requires a separately restricted private network path")
    args = parser.parse_args()
    policy = validate_policy(read_json(args.policy))
    if args.mode == "api":
        token = os.environ["CADDY_ACCESS_TOKEN"]
        if len(token) < 32 or not token.isascii():
            raise ValueError("token must contain at least 32 ASCII characters")
        class Server(ThreadingHTTPServer):
            def get_request(self):
                sock, address = super().get_request()
                sock.settimeout(10)
                return sock, address
        Server((str(args.bind), args.port), make_handler(policy, args.data_dir, args.status, token)).serve_forever()
    else:
        # Root-owned directory, never writable by the API service.
        status_dir = Path(args.status).parent
        status_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        status_dir.chmod(0o755)
        with (status_dir / "worker.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                result = reconcile(policy, read_json(Path(args.data_dir) / "allowlist.json"), Firewall())
                result["checked_at"] = utcnow().isoformat()
                atomic_json(args.status, result, mode=0o644)
            except Exception as error:
                atomic_json(args.status, {"state": "error", "error_type": type(error).__name__,
                            "checked_at": utcnow().isoformat()}, mode=0o644)
                raise SystemExit("Firewall reconciliation failed: " + type(error).__name__)


if __name__ == "__main__":
    main()
