import datetime as dt
import importlib.util
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('access', sys.argv[1])
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
run = lambda *args, **kw: subprocess.run(args, capture_output=True, text=True, **kw)
assert run('iptables', '-S', 'DOCKER-USER').returncode != 0
assert run('ip6tables', '-S', 'DOCKER-USER').returncode != 0
p = {'enabled': True, 'interface': 'eth0', 'ports': {'tcp':[80,443], 'udp':[443]},
     'always_allow':['192.168.50.0/24'], 'max_ttl_seconds':86400, 'max_entries':64}
def document(seconds):
    return {'version':1, 'revision':'smoke', 'entries':[{'id':'one', 'address':'8.8.8.8',
      'expires_at':(a.utcnow()+dt.timedelta(seconds=seconds)).isoformat()}]}
f = a.Firewall()
a.reconcile(p, document(60), f)
run('ipset','test','caddy_allow4','8.8.8.8',check=True)
run('ipset','test','caddy_allow4','192.168.50.10',check=True)
assert run('ipset','test','caddy_allow4','1.1.1.1').returncode == 1
for binary in ['iptables','ip6tables']:
    for chain in ['INPUT','DOCKER-USER']:
        rules=run(binary,'-S',chain,check=True).stdout
        assert rules.count('-j CADDY_ACCESS') == 3, rules
    run(binary,'-C','CADDY_ACCESS','-j','DROP',check=True)
a.reconcile(p, {'version':1,'revision':'revoke','entries':[]}, f)
assert run('ipset','test','caddy_allow4','8.8.8.8').returncode == 1
run('ipset','test','caddy_allow4','192.168.50.10',check=True)
a.reconcile(p, document(2), f)
run('ipset','test','caddy_allow4','8.8.8.8',check=True)
time.sleep(3)
assert run('ipset','test','caddy_allow4','8.8.8.8').returncode == 1
run('ipset','test','caddy_allow4','192.168.50.10',check=True)
print('PASS: real iptables/ip6tables rule creation, ipset matching, revoke, kernel expiry without Python worker; isolated network namespace only.')
