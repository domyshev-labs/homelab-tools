#!/usr/bin/env python3
"""Register a verifier from this checkout with externally supplied settings."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

SOURCE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('verifier',SOURCE/'backup-verifier.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

def render(config,checkout):
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',checkout):
        raise ValueError('Checkout must have no spaces or systemd special characters')
    parts=config['verification_schedule'].split()
    calendar=(parts[0].title()+' *-*-* '+parts[1] if len(parts)==2 else '*-*-* '+parts[0])+':00'
    return {
        'backup-verifier.service': ('[Unit]\nDescription=Verify configured backup artifacts and email results\n\n'
            '[Service]\nType=oneshot\n'
            f'ExecStart=/usr/bin/python3 {checkout}/backup-verifier/backup-verifier.py --config /etc/backup-verifier.json --wait --send\n'
            'StandardOutput=journal\nStandardError=journal\nSyslogIdentifier=backup-verifier\nTimeoutStartSec=infinity\n'),
        'backup-verifier.timer': ('[Unit]\nDescription=Verify backup artifacts after scheduled jobs\n\n'
            f'[Timer]\nOnCalendar={calendar}\nAccuracySec=1s\nPersistent=false\n\n[Install]\nWantedBy=timers.target\n')}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args()
    if os.geteuid()!=0: raise SystemExit('Run as root')
    config=module.validate(json.loads(args.config.read_text()))
    state=subprocess.run(['systemctl','show','backup-verifier.service','-p','ActiveState','--value'],text=True,capture_output=True).stdout.strip()
    if state not in ['inactive','failed','']: raise SystemExit('Wait for the verifier to finish')
    units=render(config,str(SOURCE.parent))
    with tempfile.TemporaryDirectory() as stage:
        for name,content in units.items(): (Path(stage)/name).write_text(content)
        subprocess.run(['systemd-analyze','verify',*[str(Path(stage)/name) for name in units]],check=True)
        parent=Path('/var/backups/backup-verifier');parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        backup=Path(tempfile.mkdtemp(prefix='before-install-',dir=parent))
        files={Path('/etc/backup-verifier.json'):(args.config.read_bytes(),0o600)}
        files.update({Path('/etc/systemd/system')/name:(content.encode(),0o644) for name,content in units.items()})
        for dest in files:
            if dest.is_symlink(): raise ValueError('Refusing symlink destination')
            if dest.exists(): shutil.copy2(dest,backup/dest.name)
        exists=subprocess.run(['systemctl','cat','backup-verifier.timer'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
        if exists: subprocess.run(['systemctl','disable','--now','backup-verifier.timer'],check=True)
        try:
            for dest,(data,mode) in files.items():
                with tempfile.NamedTemporaryFile(dir=dest.parent,delete=False) as handle:
                    temporary=Path(handle.name);os.fchmod(handle.fileno(),mode);handle.write(data)
                os.replace(temporary,dest)
            subprocess.run(['systemctl','daemon-reload'],check=True)
        except Exception:
            for dest in files:
                saved=backup/dest.name
                if saved.exists(): shutil.copy2(saved,dest)
                else: dest.unlink(missing_ok=True)
            subprocess.run(['systemctl','daemon-reload'],check=True)
            raise
        print('Previous deployment saved: '+str(backup))
        print('Verifier timer remains stopped. Test mail/settings before enabling it.')
if __name__=='__main__': main()
