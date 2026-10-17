"""Run only inside a fresh Linux network namespace; never on the host namespace."""
import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
import time

base=Path(sys.argv[1]).parent
sys.path.insert(0,str(base))
import access
import observe
run=lambda *args, **kw: subprocess.run(args,check=True,capture_output=True,text=True,**kw)
# Fresh namespaces have no Docker user chain; reject accidental host execution.
assert subprocess.run(['iptables','-S','DOCKER-USER'],capture_output=True).returncode != 0
run('ip','link','set','lo','up')
for value in ('8.8.8.8/32','1.1.1.1/32'):
    run('ip','addr','add',value,'dev','lo')
policy=dict(enabled=True,interface='lo',ports=dict(tcp=[80,443],udp=[443]),
    always_allow=['192.168.50.0/24'],max_ttl_seconds=2592000,max_entries=64,observe_attempts=True)
firewall=access.Firewall()
access.reconcile(policy,dict(version=1,revision='smoke',entries=[]),firewall)
# Direct OUTPUT hook tests only namespace-local loopback traffic.
run('iptables','-I','OUTPUT','1','-p','udp','--dport','443','-j','CADDY_ACCESS')
reader=subprocess.Popen(['tcpdump','-l','-nn','-q','-t','-i','nflog:100','-s','96','-c','1'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
time.sleep(0.7)
with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
    sender.bind(('8.8.8.8',0))
    try:
        sender.sendto(b'payload-must-not-be-recorded',('1.1.1.1',443))
    except PermissionError:
        pass  # Expected: namespace OUTPUT rule drops the packet after NFLOG.
try:
    out,err=reader.communicate(timeout=5)
except BaseException:
    reader.kill();reader.communicate();raise
assert reader.returncode==0,err
assert 'payload-must-not-be-recorded' not in out
packets=[observe.parse_line(line) for line in out.splitlines()]
assert any(packet and packet[0]=='8.8.8.8' and packet[2:]==(443,'udp') for packet in packets),repr(out)
for binary in ('iptables','ip6tables'):
    run(binary,'-C','CADDY_ACCESS','-j','DROP')
print('PASS: denied NFLOG packet observed and parsed; payload absent; terminal DROP preserved in IPv4/IPv6; fresh namespace only.')
