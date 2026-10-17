#!/usr/bin/env python3
"""Bounded, metadata-only reader of denied Caddy traffic from NFLOG group 100."""
import datetime as dt
import ipaddress
import os
from pathlib import Path
import re
import select
import subprocess
import time

from access import atomic_json, utcnow

LINE = re.compile(r'\bIP6?\s+(\S+)\s+>\s+(\S+):\s+(tcp|UDP)\b', re.IGNORECASE)


def parse_line(line):
    match = LINE.search(line)
    if not match:
        return None
    source, destination, protocol = match.groups()
    try:
        address, sport = source.rsplit('.', 1)
        _, dport = destination.rsplit('.', 1)
        address = ipaddress.ip_address(address)
        if not address.is_global or address.is_multicast:
            return None
        sport, dport = int(sport), int(dport)
        if not 1 <= sport <= 65535 or not 1 <= dport <= 65535:
            return None
    except ValueError:
        return None
    return str(address), sport, dport, protocol.lower()


class Attempts:
    def __init__(self):
        self.entries = {}
        self.flows = {}

    def prune(self, now):
        cutoff = now - dt.timedelta(days=1)
        self.entries = {ip: item for ip, item in self.entries.items() if item['last_seen'] > cutoff}
        self.flows = {key: value for key, value in self.flows.items() if value > now - dt.timedelta(seconds=60)}
        while len(self.entries) > 256:
            del self.entries[min(self.entries, key=lambda ip: self.entries[ip]['last_seen'])]
        while len(self.flows) > 4096:
            del self.flows[min(self.flows, key=self.flows.get)]

    def add(self, packet, now):
        self.prune(now)
        address, sport, dport, protocol = packet
        item = self.entries.setdefault(address, dict(address=address, first_seen=now,
            last_seen=now, count=0, port=dport, protocol=protocol))
        item['last_seen'] = now
        item['port'], item['protocol'] = dport, protocol
        if packet not in self.flows:
            item['count'] += 1
        self.flows[packet] = now
        self.prune(now)

    def document(self, now):
        self.prune(now)
        return dict(checked_at=now.isoformat(), sampled=True, retention_seconds=86400,
            entries=[dict(item, first_seen=item['first_seen'].isoformat(), last_seen=item['last_seen'].isoformat())
                for item in sorted(self.entries.values(), key=lambda item: item['last_seen'], reverse=True)])


def main():
    directory = Path('/run/caddy-access')
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    attempts = Attempts()
    # No -A/-X/-w: payload is never displayed or saved. Only parsed IP/port/times/count survive.
    process = subprocess.Popen(['tcpdump', '-l', '-nn', '-q', '-t', '-s', '96', '-i', 'nflog:100'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    pending = b''
    try:
        while process.poll() is None:
            if select.select([process.stdout], [], [], 1)[0]:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
                lines = pending.split(b'\n')
                pending = lines.pop()[-4096:]
                now = utcnow()
                for line in lines:
                    packet = parse_line(line.decode('ascii', errors='replace'))
                    if packet:
                        attempts.add(packet, now)
            atomic_json(directory / 'attempts.json', attempts.document(utcnow()), mode=0o644)
            time.sleep(0.1)
    finally:
        process.terminate()
        process.wait(timeout=5)
    raise SystemExit('NFLOG reader stopped')


if __name__ == '__main__':
    main()
