import datetime as dt
import http.client
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

TOOL = Path(__file__).resolve().parents[1] / 'caddy-access'
spec = importlib.util.spec_from_file_location('access', TOOL / 'access.py')
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)
sys.modules['access'] = access
spec = importlib.util.spec_from_file_location('observe', TOOL / 'observe.py')
observe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observe)


class APIIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.policy = access.read_json(TOOL / 'policy.example.json')
        self.policy.update(max_ttl_seconds=2592000, enabled=True)
        access.atomic_json(self.path / 'allowlist.json', dict(version=1, revision='initial', entries=[]))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), access.make_handler(self.policy, self.path, self.path / 'status.json', 'x'*48))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, auth=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'Authorization': 'Bearer ' + 'x'*48} if auth else {}
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_long_lease_and_empty_label(self):
        for ttl in (604800, 2592000):
            status, result = self.request('POST', '/v1/entries', dict(address='8.8.8.8', ttl_seconds=ttl))
            self.assertEqual(status, 202)
            self.assertEqual(result['entry']['label'], '')
            remaining = (access.timestamp(result['entry']['expires_at']) - access.utcnow()).total_seconds()
            self.assertTrue(ttl-3 < remaining <= ttl)
        self.assertEqual(self.request('POST', '/v1/entries', dict(address='1.1.1.1', ttl_seconds=2592001))[0], 400)

    def test_rename_keeps_expiry_and_requires_auth(self):
        _, result = self.request('POST', '/v1/entries', dict(address='8.8.8.8'))
        entry = result['entry']
        status, renamed = self.request('PATCH', '/v1/entries/' + entry['id'], dict(label='Office'))
        self.assertEqual(status, 202)
        self.assertEqual(renamed['entry']['expires_at'], entry['expires_at'])
        self.assertEqual(renamed['entry']['label'], 'Office')
        self.assertEqual(self.request('PATCH', '/v1/entries/' + entry['id'], dict(label=''), auth=False)[0], 401)
        self.assertEqual(self.request('PATCH', '/v1/entries/' + entry['id'], dict(label='', ttl_seconds=123))[0], 400)

    def test_expired_history_survives_mutation_can_renew_and_delete(self):
        expired = dict(id='old', address='8.8.8.8', label='Saved', expires_at=(access.utcnow()-dt.timedelta(days=1)).isoformat())
        access.atomic_json(self.path / 'allowlist.json', dict(version=1, revision='old', entries=[expired]))
        self.assertEqual(self.request('GET', '/v1/entries')[1]['entries'], [])
        self.assertEqual(self.request('GET', '/v1/entries?include_expired=1')[1]['entries'], [expired])
        self.request('POST', '/v1/entries', dict(address='1.1.1.1'))
        self.assertEqual(len(self.request('GET', '/v1/entries?include_expired=1')[1]['entries']), 2)
        self.assertEqual(self.request('PATCH', '/v1/entries/old', dict(label='New'))[1]['entry']['expires_at'], expired['expires_at'])
        self.assertEqual(self.request('DELETE', '/v1/entries/old')[0], 202)
        self.assertEqual(len(self.request('GET', '/v1/entries?include_expired=1')[1]['entries']), 1)
        _, new = self.request('POST', '/v1/entries', dict(address='8.8.8.8', ttl_seconds=604800))
        self.assertGreater(access.timestamp(new['entry']['expires_at']), access.utcnow())

    def test_attempts_authenticated_pruned_and_no_grant_side_effect(self):
        old = dict(address='8.8.8.8', last_seen=(access.utcnow()-dt.timedelta(days=2)).isoformat())
        recent = dict(address='1.1.1.1', last_seen=access.utcnow().isoformat(), count=2, port=443, protocol='tcp')
        access.atomic_json(self.path / 'attempts.json', dict(entries=[old,recent], checked_at=access.utcnow().isoformat()), mode=0o644)
        self.assertEqual(self.request('GET', '/v1/attempts', auth=False)[0], 401)
        self.assertEqual(self.request('GET', '/v1/attempts')[1]['entries'], [recent])
        self.assertEqual(self.request('GET', '/v1/entries')[1]['entries'], [])

    def test_block_unblock_preserves_original_lease_and_delete_removes(self):
        _, added = self.request('POST', '/v1/entries', dict(address='8.8.8.8'))
        entry = added['entry']
        path = '/v1/entries/' + entry['id']
        self.assertEqual(self.request('PATCH', path, dict(blocked=True))[0], 202)
        self.assertEqual(self.request('GET', '/v1/entries')[1]['entries'], [])
        history = self.request('GET', '/v1/entries?include_expired=1')[1]['entries']
        self.assertTrue(history[0]['blocked'])
        self.assertEqual(history[0]['expires_at'], entry['expires_at'])
        self.assertEqual(self.request('PATCH', path, dict(blocked=False))[0], 202)
        self.assertEqual(len(self.request('GET', '/v1/entries')[1]['entries']), 1)
        self.assertEqual(self.request('PATCH', path, dict(blocked='false'))[0], 400)
        self.request('PATCH', path, dict(blocked=True))
        self.assertEqual(self.request('DELETE', path)[0], 202)
        self.assertEqual(self.request('GET', '/v1/entries?include_expired=1')[1]['entries'], [])

    def test_thirty_day_kernel_timeout_is_capped_fail_closed(self):
        class Fake:
            def __init__(self): self.values = []
            def apply_set(self, name, family, members): self.values += members
            def attach(self, *args): pass
        self.request('POST', '/v1/entries', dict(address='8.8.8.8', ttl_seconds=2592000))
        fake = Fake()
        access.reconcile(self.policy, access.read_json(self.path / 'allowlist.json'), fake)
        self.assertIn(('8.8.8.8/32', 2147483), fake.values)

    def test_history_evicts_expired_but_never_active(self):
        self.policy['max_entries'] = 1
        expired = dict(id='old', address='8.8.8.8', expires_at=(access.utcnow()-dt.timedelta(days=1)).isoformat())
        access.atomic_json(self.path / 'allowlist.json', dict(version=1, revision='old', entries=[expired]))
        self.assertEqual(self.request('POST', '/v1/entries', dict(address='1.1.1.1'))[0], 202)
        self.assertEqual(self.request('POST', '/v1/entries', dict(address='8.8.8.8'))[0], 400)
        self.assertEqual(self.request('GET', '/v1/entries')[1]['entries'][0]['address'], '1.1.1.1')


class ObserverTests(unittest.TestCase):
    def test_parse_ipv4_ipv6_and_private_rejection(self):
        self.assertEqual(observe.parse_line('IP 8.8.8.8.12345 > 192.168.1.2.443: tcp 0'), ('8.8.8.8',12345,443,'tcp'))
        self.assertEqual(observe.parse_line('IP6 2606:4700:4700::1111.123 > fd00::1.443: UDP, length 10'), ('2606:4700:4700::1111',123,443,'udp'))
        self.assertIsNone(observe.parse_line('IP 192.168.1.10.123 > 192.168.1.2.443: tcp 0'))
        self.assertIsNone(observe.parse_line('arbitrary payload'))

    def test_deduplicates_retransmissions_retention_and_bounds(self):
        tracker = observe.Attempts()
        now = access.utcnow()
        packet = ('8.8.8.8',123,443,'tcp')
        tracker.add(packet, now)
        tracker.add(packet, now+dt.timedelta(seconds=1))
        tracker.add(('8.8.8.8',124,443,'tcp'), now+dt.timedelta(seconds=2))
        self.assertEqual(tracker.document(now+dt.timedelta(seconds=3))['entries'][0]['count'], 2)
        self.assertEqual(tracker.document(now+dt.timedelta(days=2))['entries'], [])
        for n in range(400):
            tracker.add(('8.8.%d.%d' % (n//256,n%256),123,443,'tcp'), now)
        self.assertEqual(len(tracker.document(now)['entries']),256)

    def test_logging_cannot_allow_or_log_trusted_sources(self):
        from unittest.mock import Mock
        firewall = access.Firewall()
        calls=[]
        firewall.run=lambda *args, **kw: (calls.append(args) or Mock(returncode=1 if '-C' in args or '-S' in args else 0))
        policy=access.read_json(TOOL/'policy.example.json')
        policy['observe_attempts']=True
        firewall.attach('iptables','caddy_allow4',policy)
        logging=[call for call in calls if '-I' in call and 'NFLOG' in call]
        self.assertEqual(len(logging),2)
        for call in logging:
            self.assertIn('!',call)
            self.assertIn('caddy_allow4',call)
            self.assertIn('--limit',call)
        self.assertTrue(any('-I' in call and call[-1]=='DROP' for call in calls))
