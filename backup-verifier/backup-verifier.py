#!/usr/bin/env python3
"""Verify configured Proxmox backup artifacts and send a two-level HTML report."""
import argparse
import datetime as dt
from email.message import EmailMessage
from fractions import Fraction
import fcntl
import html
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import stat
import subprocess
import sys
import time

DAYS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
COLORS = {'SUCCESS':'#15803d','WARNING':'#b45309','FAIL':'#b91c1c'}

def log(level, event, message, **fields):
    print(json.dumps(dict(timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
        service='backup-verifier',level=level,event=event,message=message,**fields),ensure_ascii=False),flush=True)

def command(args, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Command failed: '+args[0])
    return result.stdout

def validate(config):
    if set(config) != {'host','vm','stability_seconds','wait_timeout_seconds','verification_schedule','smtp'}:
        raise ValueError('Unexpected verifier configuration fields')
    last_due(config['verification_schedule'],dt.datetime.now().astimezone())
    host,vm=config['host'],config['vm']
    if set(host) != {'config_file','required_members'} or set(vm) != {'job_id','dataset','directory'}:
        raise ValueError('Unexpected job fields')
    for path in [host['config_file'],vm['directory']]:
        if not isinstance(path,str) or not path.startswith('/') or '..' in Path(path).parts or any(ord(c)<32 for c in path):
            raise ValueError('Use absolute paths without control characters or ..')
    if not isinstance(host['required_members'],list) or any(not isinstance(p,str) or not p or p.startswith('/') or '..' in Path(p).parts for p in host['required_members']):
        raise ValueError('required_members must contain archive-relative paths')
    for key in ['stability_seconds','wait_timeout_seconds']:
        if type(config[key]) is not int or not 0<=config[key]<=14400:
            raise ValueError('Invalid '+key)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',vm['job_id']) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:/-]*',vm['dataset']):
        raise ValueError('Invalid VM job or dataset')
    smtp=config['smtp']
    if set(smtp) != {'host','port','username','from','to','password_file','subject_prefix'}:
        raise ValueError('Unexpected SMTP fields')
    for key in ['host','username','from','password_file','subject_prefix']:
        if not isinstance(smtp[key],str) or not smtp[key] or any(ord(c)<32 for c in smtp[key]):
            raise ValueError('Invalid SMTP '+key)
    if type(smtp['port']) is not int or not 1<=smtp['port']<=65535:
        raise ValueError('Invalid SMTP port')
    if not isinstance(smtp['to'],list) or not smtp['to'] or any(not isinstance(v,str) or '@' not in v or any(ord(c)<32 for c in v) for v in smtp['to']):
        raise ValueError('Invalid email recipients')
    if not smtp['password_file'].startswith('/'):
        raise ValueError('password_file must be absolute')
    return config

def last_due(schedule, now):
    match=re.fullmatch(r'(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun) )?([0-2][0-9]):([0-5][0-9])',schedule,re.IGNORECASE)
    if not match or int(match[2])>23:
        raise ValueError('Only daily HH:MM or weekday HH:MM schedules are supported')
    candidate=now.replace(hour=int(match[2]),minute=int(match[3]),second=0,microsecond=0)
    if match[1]:
        weekday=DAYS.index(match[1].title())
        candidate-=dt.timedelta(days=(now.weekday()-weekday)%7)
        if candidate>now: candidate-=dt.timedelta(days=7)
    elif candidate>now:
        candidate-=dt.timedelta(days=1)
    return candidate.timestamp()

def mounted(directory, dataset):
    root=Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError('Backup directory missing or symlinked: '+directory)
    source=command(['findmnt','-n','-T',directory,'-o','SOURCE']).strip()
    fstype=command(['findmnt','-n','-T',directory,'-o','FSTYPE']).strip()
    if source!=dataset or fstype!='zfs':
        raise RuntimeError('Expected ZFS dataset '+dataset+' at '+directory)
    return root

def inspect_archive(root, pattern, cutoff, stability, full=False, required=()):
    matches=[p for p in root.glob(pattern) if p.is_file() and not p.is_symlink()]
    if not matches:
        return dict(success=False,reason='Archive missing',archive=None,size=0)
    path=max(matches,key=lambda p:p.stat().st_mtime)
    before=path.stat()
    details=dict(archive=str(path),size=before.st_size,expected_after=dt.datetime.fromtimestamp(cutoff,dt.timezone.utc).isoformat(),modified=dt.datetime.fromtimestamp(before.st_mtime,dt.timezone.utc).isoformat())
    if before.st_size<=0:
        return dict(details,success=False,reason='Archive is empty')
    if before.st_mtime<cutoff:
        return dict(details,success=False,reason='Archive is older than the expected backup run')
    if before.st_mtime>time.time()+300:
        return dict(details,success=False,reason='Archive timestamp is in the future')
    if not os.access(path,os.R_OK):
        return dict(details,success=False,reason='Archive is not readable')
    if stability: time.sleep(stability)
    after=path.stat()
    if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
        return dict(details,success=False,reason='Archive is still changing')
    partials=[p for p in root.glob(pattern+'*') if p.name.endswith(('.part','.tmp')) and p.stat().st_mtime>=cutoff]
    if partials:
        return dict(details,success=False,reason='Unfinished archive files remain')
    try:
        if full or required:
            command(['zstd','-q','-t','--',str(path)],timeout=14400)
        if required:
            members=set(command(['tar','-I','zstd','-tf',str(path)],timeout=120).splitlines())
            missing=[name for name in required if name not in members]
            if missing:
                return dict(details,success=False,reason='Required archive members missing: '+', '.join(missing))
    except (RuntimeError,subprocess.TimeoutExpired):
        return dict(details,success=False,reason='Archive integrity or structure check failed')
    final=path.stat()
    if (after.st_ino,after.st_size,after.st_mtime_ns)!=(final.st_ino,final.st_size,final.st_mtime_ns):
        return dict(details,success=False,reason='Archive changed during verification')
    return dict(details,success=True,reason='Fresh, nonempty, stable archive'+('; integrity and required members verified' if required else '; compression verified' if full else ''))

def selected_guests(job, resources, node):
    local={int(v['vmid']):v for v in resources if v.get('node')==node and v.get('type') in ['qemu','lxc']}
    if job.get('pool'):
        raise ValueError('Pool-based VM selection is not supported; use explicit vmid or all/exclude')
    if job.get('vmid'):
        ids=[int(v) for v in re.split(r'[,;\s]+',str(job['vmid'])) if v]
    elif job.get('all'):
        excluded={int(v) for v in re.split(r'[,;\s]+',str(job.get('exclude',''))) if v}
        ids=sorted(set(local)-excluded)
    else:
        raise ValueError('Backup job has no supported guest selection')
    if not ids or len(ids)!=len(set(ids)):
        raise ValueError('Backup job guest list is empty or duplicated')
    return [(vmid,local.get(vmid)) for vmid in sorted(ids)]

def outcome(name, rows):
    successes=sum(row['success'] for row in rows)
    score=Fraction(100*successes,len(rows)) if rows else Fraction(0)
    return dict(name=name,rows=rows,successes=successes,total=len(rows),score=score)

def collect(config, now, full=False, cutoff_override=None):
    jobs=[]
    try:
        host=json.loads(Path(config['host']['config_file']).read_text())
        cutoff=cutoff_override if cutoff_override is not None else last_due(host['schedule'],now)
        root=mounted(host['backup_dir'],host['backup_dataset'])
        prefix=command(['hostname','-s']).strip()
        row=inspect_archive(root,prefix+'-config-*.tar.zst',cutoff,config['stability_seconds'],full,config['host']['required_members'])
        row.update(name='Proxmox host configuration')
    except (ValueError,KeyError,OSError,RuntimeError,subprocess.TimeoutExpired):
        row=dict(name='Proxmox host configuration',success=False,reason='Host configuration or destination could not be inspected',archive=None,size=0)
    jobs.append(outcome('Proxmox configuration backup',[row]))
    try:
        job=json.loads(command(['pvesh','get','/cluster/backup/'+config['vm']['job_id'],'--output-format','json']))
        resources=json.loads(command(['pvesh','get','/cluster/resources','--type','vm','--output-format','json']))
        node=command(['hostname','-s']).strip()
        guests=selected_guests(job,resources,node)
        cutoff=cutoff_override if cutoff_override is not None else last_due(job['schedule'],now)
        try:
            root=mounted(config['vm']['directory'],config['vm']['dataset'])
            mount_error=False
        except (RuntimeError,OSError):
            mount_error=True
        rows=[]
        for vmid,resource in guests:
            if not resource:
                row=dict(success=False,reason='Expected guest not found on this node',archive=None,size=0)
            elif mount_error:
                row=dict(success=False,reason='VM backup destination not mounted correctly',archive=None,size=0)
            else:
                kind=resource['type']
                extension='vma.zst' if kind=='qemu' else 'tar.zst'
                row=inspect_archive(root,f'vzdump-{kind}-{vmid}-*.{extension}',cutoff,config['stability_seconds'],full)
            row.update(name=f'VM {vmid}' + (' — ' + resource['name'] if resource and resource.get('name') else ''),vmid=vmid)
            rows.append(row)
    except (ValueError,KeyError,OSError,RuntimeError,subprocess.TimeoutExpired):
        rows=[dict(name='Expected VM list unavailable',success=False,reason='Proxmox backup job or resource query failed; no empty-list success',archive=None,size=0)]
    jobs.append(outcome('Virtual machine backups',rows))
    return jobs

def total_score(jobs):
    return sum((job['score'] for job in jobs),Fraction(0))/len(jobs) if jobs else Fraction(0)

def status(score):
    return 'SUCCESS' if score==100 else 'FAIL' if score==0 else 'WARNING'

def percent(score):
    if score.denominator==1: return str(score.numerator)
    return f'{float(score):.3f}'.rstrip('0').rstrip('.')

def subject(jobs,prefix):
    score=total_score(jobs)
    return f'{status(score)} {percent(score)}% — {prefix}'

def report_html(jobs, title, generated_at):
    score=total_score(jobs);state=status(score);color=COLORS[state]
    esc=lambda value:html.escape(str(value),quote=True)
    badge=lambda state:f'<strong style="color:{COLORS[state]};">{state}</strong>'
    cell='padding:10px;border:1px solid #d1d5db;vertical-align:top;'
    parts=[f'<!doctype html><html><body style="font-family:Arial,sans-serif;color:#111827;"><div style="max-width:760px;margin:auto;">'
        f'<h2>{esc(title)}</h2><div style="padding:20px;background:#f3f4f6;border-left:8px solid {color};">'
        f'<div style="font-size:32px;font-weight:bold;color:{color};">{state} {percent(score)}%</div>'
        f'<div>Overall result: average of the two job percentages.</div></div>'
        f'<p>{esc(generated_at)}</p><table style="width:100%;border-collapse:collapse;"><thead><tr>'
        f'<th style="{cell}">Job</th><th style="{cell}">Job result</th><th style="{cell}">Item</th>'
        f'<th style="{cell}">Status</th><th style="{cell}">Details</th></tr></thead><tbody>']
    for job in jobs:
        count=max(1,len(job['rows']))
        for index,row in enumerate(job['rows']):
            parts.append('<tr>')
            if index==0:
                parts.append(f'<td rowspan="{count}" style="{cell}"><strong>{esc(job["name"])}</strong></td>'
                    f'<td rowspan="{count}" style="{cell}">{badge(status(job["score"]))}<br><strong>{percent(job["score"])}%</strong><br>{job["successes"]}/{job["total"]}</td>')
            detail=row['reason']
            if row.get('archive'): detail+=' — '+Path(row['archive']).name+f' ({row["size"]:,} bytes)'
            if row.get('expected_after'): detail+='; expected after '+row['expected_after']
            parts.append(f'<td style="{cell}">{esc(row["name"])}</td><td style="{cell}">{badge("SUCCESS" if row["success"] else "FAIL")}</td><td style="{cell}">{esc(detail)}</td></tr>')
    parts.append('</tbody></table><p style="font-size:12px;color:#6b7280;">File verification covers the selected checks. A test restore remains a separate recovery check.</p></div></body></html>')
    return ''.join(parts)

def report_text(jobs):
    score=total_score(jobs)
    lines=[f'{status(score)} {percent(score)}%','Overall = average of job percentages.','']
    for job in jobs:
        lines.append(f'{job["name"]}: {status(job["score"])} {percent(job["score"])}% ({job["successes"]}/{job["total"]})')
        lines += [f'  {row["name"]}: {"SUCCESS" if row["success"] else "FAIL"} — {row["reason"]}' for row in job['rows']]
    return '\n'.join(lines)

def make_message(jobs,smtp,now):
    msg=EmailMessage()
    msg['From']=smtp['from'];msg['To']=', '.join(smtp['to']);msg['Subject']=subject(jobs,smtp['subject_prefix'])
    msg.set_content(report_text(jobs))
    msg.add_alternative(report_html(jobs,smtp['subject_prefix'],now.isoformat()),subtype='html')
    return msg

def send_report(message,smtp):
    secret=Path(smtp['password_file'])
    if secret.is_symlink() or not secret.is_file() or stat.S_IMODE(secret.stat().st_mode)&0o077:
        raise RuntimeError('Password file must be a regular private file with mode 0600')
    password=secret.read_text().strip().replace(' ','')
    if not password: raise RuntimeError('Password file is empty')
    with smtplib.SMTP(smtp['host'],smtp['port'],timeout=30) as server:
        server.ehlo();server.starttls(context=ssl.create_default_context());server.ehlo()
        server.login(smtp['username'],password)
        rejected=server.send_message(message)
        if rejected: raise RuntimeError('SMTP rejected a report recipient')

def wait_until_idle(timeout):
    end=time.monotonic()+timeout
    while True:
        active=json.loads(command(['pvenode','task','list','--source','active','--output-format','json']))
        host=command(['systemctl','show','pve-config-backup.service','-p','ActiveState','--value']).strip()
        if not any(task.get('type')=='vzdump' for task in active) and host not in ['active','activating','deactivating']:
            return
        if time.monotonic()>=end: raise RuntimeError('Backups did not finish before verification timeout')
        time.sleep(10)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--validate-config',action='store_true')
    parser.add_argument('--full',action='store_true')
    parser.add_argument('--send',action='store_true')
    parser.add_argument('--wait',action='store_true')
    parser.add_argument('--not-before',help='ISO timestamp override for an explicitly chosen historical run')
    parser.add_argument('--html',type=Path,help='Save a local report preview; no email without --send')
    args=parser.parse_args()
    config=validate(json.loads(args.config.read_text()))
    if args.validate_config:
        print('Configuration valid');return 0
    now=dt.datetime.now().astimezone()
    cutoff=None
    if args.not_before:
        parsed=dt.datetime.fromisoformat(args.not_before)
        if parsed.tzinfo is None: raise ValueError('--not-before must include a timezone')
        cutoff=parsed.timestamp()
        config['smtp']=dict(config['smtp'],subject_prefix=config['smtp']['subject_prefix']+' — historical check since '+parsed.isoformat())
        log('info','historical_cutoff','Explicit historical verification; regular scheduled runs use current job schedules',not_before=parsed.isoformat())
    with open('/run/lock/zfs-disk-standby-scheduler.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_SH)
        with open('/run/lock/backup-verifier.lock','a') as unique:
            fcntl.flock(unique,fcntl.LOCK_EX)
            idle_error=None
            try:
                if args.wait: wait_until_idle(config['wait_timeout_seconds'])
                jobs=collect(config,now,args.full,cutoff)
            except (ValueError,RuntimeError,subprocess.TimeoutExpired) as error:
                idle_error=type(error).__name__
                jobs=[outcome(name,[dict(name=name,success=False,reason='Could not establish that backups are idle',archive=None,size=0)]) for name in ['Proxmox configuration backup','Virtual machine backups']]
            # Keep the storage lock through archive checks only, then allow shutdown.
            fcntl.flock(lock,fcntl.LOCK_UN)
            for job in jobs:
                log('info' if job['score']==100 else 'error' if job['score']==0 else 'warn','job_verified',job['name'],percentage=percent(job['score']),successes=job['successes'],expected=job['total'],items=job['rows'])
            score=total_score(jobs)
            log('info' if score==100 else 'error' if score==0 else 'warn','verification_completed',subject(jobs,config['smtp']['subject_prefix']),percentage=percent(score),status=status(score),idle_error=idle_error)
            if args.html: args.html.write_text(report_html(jobs,config['smtp']['subject_prefix'],now.isoformat()))
            if args.send:
                try:
                    send_report(make_message(jobs,config['smtp'],now),config['smtp'])
                    log('info','report_sent','SMTP accepted the report',recipient_count=len(config['smtp']['to']))
                except Exception as error:
                    log('error','mail_failed','Report delivery failed',error_type=type(error).__name__)
                    return 2
            return 0 if score==100 else 1

if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (ValueError,OSError,RuntimeError,KeyError) as error:
        log('error','verification_error','Verifier could not run',error_type=type(error).__name__)
        raise SystemExit(2)
