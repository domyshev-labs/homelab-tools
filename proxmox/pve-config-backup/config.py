#!/usr/bin/env python3
"""Validate configuration and render portable deployment files. No host operations."""
import argparse
import json
from pathlib import Path
import re
import shlex

FIELDS = {'backup_dir', 'backup_dataset', 'retention_days', 'compression_level',
          'schedule', 'randomized_delay_seconds', 'persistent'}

def validate(config):
    if not isinstance(config, dict) or set(config) != FIELDS:
        raise ValueError('Expected exactly: ' + ', '.join(sorted(FIELDS)))
    path = config['backup_dir']
    if not isinstance(path, str) or not path.startswith('/') or path == '/' or '..' in Path(path).parts or any(ord(c) < 32 for c in path):
        raise ValueError('backup_dir must be an absolute directory, without .. or control characters')
    if not isinstance(config['backup_dataset'], str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:/-]*', config['backup_dataset']) or '..' in config['backup_dataset'].split('/'):
        raise ValueError('backup_dataset must be a ZFS dataset name')
    for key, lo, hi in [('retention_days', 1, 36500), ('compression_level', 1, 19), ('randomized_delay_seconds', 0, 86400)]:
        if type(config[key]) is not int or not lo <= config[key] <= hi:
            raise ValueError(f'{key} must be an integer between {lo} and {hi}')
    if not isinstance(config['schedule'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', config['schedule']):
        raise ValueError('schedule must use HH:MM')
    if type(config['persistent']) is not bool:
        raise ValueError('persistent must be a boolean')
    return config

def render_units(config, checkout):
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', checkout) or '..' in Path(checkout).parts:
        raise ValueError('checkout must be an absolute path without spaces or systemd special characters')
    return {
        'pve-config-backup.service': ('[Unit]\nDescription=Backup Proxmox VE host configuration\n\n'
            '[Service]\nType=oneshot\n'
            f'ExecStart=/bin/bash {checkout}/proxmox/pve-config-backup/pve-config-backup --config /etc/pve-config-backup.json\n'
            'StandardOutput=journal\nStandardError=journal\nSyslogIdentifier=pve-config-backup\n'),
        'pve-config-backup.timer': ('[Unit]\nDescription=Scheduled Proxmox host configuration backup\n\n'
            f'[Timer]\nOnCalendar=*-*-* {config["schedule"]}:00\n'
            f'RandomizedDelaySec={config["randomized_delay_seconds"]}s\n'
            f'Persistent={str(config["persistent"]).lower()}\n\n[Install]\nWantedBy=timers.target\n')}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['validate', 'shell', 'units'])
    parser.add_argument('config', type=Path)
    parser.add_argument('--checkout')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    config = validate(json.loads(args.config.read_text()))
    if args.mode == 'shell':
        for variable, key in [('BACKUP_DIR','backup_dir'),('BACKUP_DATASET','backup_dataset'),('RETENTION_DAYS','retention_days'),('COMPRESSION_LEVEL','compression_level')]:
            print(variable + '=' + shlex.quote(str(config[key])))
    elif args.mode == 'units':
        if not args.checkout or not args.output:
            parser.error('units requires --checkout and --output')
        args.output.mkdir(parents=True, exist_ok=True)
        for name, content in render_units(config, args.checkout).items():
            (args.output/name).write_text(content)
    else:
        print('Configuration valid')

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
