import datetime as dt
from fractions import Fraction
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('verifier',ROOT/'backup-verifier/backup-verifier.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config=json.loads((ROOT/'backup-verifier/backup-verifier.example.json').read_text())

    def jobs(self,vm_successes=5,host_success=True):
        return [m.outcome('Host config',[dict(name='Host',success=host_success,reason='fixture')]),m.outcome('Virtual machines',[dict(name=f'VM {n}',success=n<vm_successes,reason='fixture') for n in range(5)])]

    def test_equal_job_weighting(self):
        jobs=self.jobs(4)
        self.assertEqual(jobs[1]['score'],80)
        self.assertEqual(m.total_score(jobs),90)
        self.assertTrue(m.subject(jobs,'Test').startswith('WARNING 90%'))

    def test_subject_boundaries(self):
        self.assertTrue(m.subject(self.jobs(),'Test').startswith('SUCCESS 100%'))
        self.assertTrue(m.subject(self.jobs(0,False),'Test').startswith('FAIL 0%'))
        self.assertTrue(m.subject(self.jobs(0,True),'Test').startswith('WARNING 50%'))

    def test_empty_job_is_zero_not_success(self):
        self.assertEqual(m.outcome('Unavailable',[])['score'],0)

    def test_html_groups_vm_rows_and_colors_and_escapes(self):
        jobs=self.jobs(4);jobs[1]['rows'][0]['name']='<script>bad</script>'
        body=m.report_html(jobs,'Example','2026-10-12')
        self.assertIn('rowspan="5"',body)
        self.assertIn('WARNING 90%',body)
        for color in m.COLORS.values():self.assertIn(color,body)
        self.assertNotIn('<script>',body)

    def test_message_has_html_and_plaintext(self):
        msg=m.make_message(self.jobs(),self.config['smtp'],dt.datetime.now(dt.timezone.utc))
        self.assertEqual(msg.get_content_type(),'multipart/alternative')
        self.assertEqual([p.get_content_type() for p in msg.iter_parts()],['text/plain','text/html'])

    def test_missing_empty_and_stale_archives(self):
        self.assertFalse(m.inspect_archive(self.root,'*.zst',0,0)['success'])
        path=self.root/'a.zst';path.touch()
        self.assertIn('empty',m.inspect_archive(self.root,'*.zst',0,0)['reason'])
        path.write_bytes(b'fixture');os.utime(path,(1,1))
        self.assertIn('older',m.inspect_archive(self.root,'*.zst',2,0)['reason'])

    def test_sidecars_do_not_count_as_archive(self):
        (self.root/'a.vma.zst.notes').write_text('fixture')
        self.assertFalse(m.inspect_archive(self.root,'*.vma.zst',0,0)['success'])

    def test_recent_partial_file_fails_final_archive(self):
        (self.root/'a.vma.zst').write_bytes(b'fixture')
        (self.root/'a.vma.zst.part').write_bytes(b'partial')
        self.assertIn('Unfinished',m.inspect_archive(self.root,'*.vma.zst',0,0)['reason'])

    def test_fast_guest_check_does_not_read_entire_compression(self):
        (self.root/'a.vma.zst').write_bytes(b'fixture')
        with patch.object(m,'command',side_effect=AssertionError('fast mode must not decompress')):
            self.assertTrue(m.inspect_archive(self.root,'*.vma.zst',0,0)['success'])

    def test_config_compression_and_required_members(self):
        (self.root/'host.tar.zst').write_bytes(b'fixture')
        with patch.object(m,'command',side_effect=['','etc/pve/jobs.cfg\n']):
            result=m.inspect_archive(self.root,'*.tar.zst',0,0,required=['var/lib/pve-cluster/config.db'])
        self.assertFalse(result['success']);self.assertIn('Required',result['reason'])

    def test_corrupt_compression_fails(self):
        (self.root/'a.vma.zst').write_bytes(b'fixture')
        with patch.object(m,'command',side_effect=RuntimeError('fixture')):
            self.assertFalse(m.inspect_archive(self.root,'*.vma.zst',0,0,full=True)['success'])

    def test_weekly_due_time(self):
        now=dt.datetime(2026,10,19,2,40,tzinfo=dt.timezone(dt.timedelta(hours=2)))
        self.assertEqual(dt.datetime.fromtimestamp(m.last_due('mon 02:25',now),now.tzinfo).hour,2)
        self.assertEqual(dt.datetime.fromtimestamp(m.last_due('mon 02:25',now),now.tzinfo).day,19)
        self.assertEqual(dt.datetime.fromtimestamp(m.last_due('mon 02:25',now.replace(hour=1)),now.tzinfo).day,12)

    def test_guest_selection_tracks_job_not_static_fixture_list(self):
        resources=[dict(vmid=n,type='qemu',node='node') for n in [101,103,106]]
        self.assertEqual([n for n,_ in m.selected_guests(dict(vmid='103,106'),resources,'node')],[103,106])
        self.assertEqual([n for n,_ in m.selected_guests(dict(all=1,exclude='101'),resources,'node')],[103,106])
        self.assertIsNone(m.selected_guests(dict(vmid='999'),resources,'node')[0][1])

    def test_gmail_secret_is_used_but_not_in_message(self):
        secret=self.root/'secret';secret.write_text('FAKE_TEST_PASSWORD');secret.chmod(0o600)
        smtp=dict(self.config['smtp'],password_file=str(secret))
        msg=m.make_message(self.jobs(),smtp,dt.datetime.now(dt.timezone.utc))
        with patch.object(m.smtplib,'SMTP') as factory:
            server=factory.return_value.__enter__.return_value
            server.send_message.return_value={}
            m.send_report(msg,smtp)
            server.starttls.assert_called_once()
            server.login.assert_called_once_with(smtp['username'],'FAKE_TEST_PASSWORD')
        self.assertNotIn('FAKE_TEST_PASSWORD',msg.as_string())

    def test_password_requires_private_permissions(self):
        secret=self.root/'secret';secret.write_text('FAKE_TEST_PASSWORD');secret.chmod(0o644)
        with self.assertRaises(RuntimeError):m.send_report(m.make_message(self.jobs(),self.config['smtp'],dt.datetime.now(dt.timezone.utc)),dict(self.config['smtp'],password_file=str(secret)))

    def test_configuration_validation(self):
        m.validate(self.config)
        with self.assertRaises(ValueError):m.validate(dict(self.config,verification_schedule='mon 88:25'))

if __name__=='__main__':unittest.main()
