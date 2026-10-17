import copy
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

TOOL = Path(__file__).resolve().parents[1] / "caddy-access"
SPEC = importlib.util.spec_from_file_location("caddy_access", TOOL / "access.py")
access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(access)


class FirewallFake:
    def __init__(self):
        self.sets = {}
        self.attached = []

    def apply_set(self, name, family, members):
        self.sets[name] = (family, dict(members))

    def attach(self, binary, name, policy):
        self.attached.append(binary)


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.policy = access.read_json(TOOL / "policy.example.json")
        self.policy["enabled"] = True
        self.now = access.utcnow()
        self.document = {"version": 1, "revision": "r1", "entries": []}

    def entry(self, address="8.8.8.8", seconds=3600):
        return {"id": "test", "address": address, "label": "Office",
                "expires_at": (self.now + dt.timedelta(seconds=seconds)).isoformat()}

    def test_ip_validation_blocks_networks_private_multicast_and_shell(self):
        for value in ["192.168.1.10", "127.0.0.1", "100.64.0.1", "::1", "ff0e::1",
                      "8.8.8.0/24", "8.8.8.8;id", "8.8.8.8\nflush"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                access.public_address(value)
        self.assertEqual(access.public_address("2606:4700:4700::1111"), "2606:4700:4700::1111")

    def test_disabled_has_no_firewall_effect(self):
        self.policy["enabled"] = False
        fake = FirewallFake()
        result = access.reconcile(self.policy, self.document, fake, self.now)
        self.assertEqual(result["state"], "disabled")
        self.assertEqual(fake.sets, {})

    def test_expiry_and_both_families(self):
        self.document["entries"] = [self.entry()]
        ipv6 = self.entry("2606:4700:4700::1111", 100)
        ipv6["id"] = "v6"
        expired = self.entry("1.1.1.1", -1)
        expired["id"] = "expired"
        self.document["entries"] += [ipv6, expired]
        fake = FirewallFake()
        access.reconcile(self.policy, self.document, fake, self.now)
        self.assertEqual(fake.sets["caddy_allow4"][1], {"192.168.50.0/24": 0, "8.8.8.8/32": 3600})
        self.assertEqual(fake.sets["caddy_allow6"][1], {"2606:4700:4700::1111/128": 100})
        fake = FirewallFake()
        access.reconcile(self.policy, self.document, fake, self.now + dt.timedelta(seconds=60))
        self.assertEqual(fake.sets["caddy_allow6"][1]["2606:4700:4700::1111/128"], 40)

    def test_root_worker_rejects_untrusted_long_lifetime_before_mutation(self):
        self.document["entries"] = [self.entry(seconds=86460)]
        fake = FirewallFake()
        with self.assertRaises(ValueError):
            access.reconcile(self.policy, self.document, fake, self.now)
        self.assertFalse(fake.sets)

    def test_restore_failure_does_not_swap_live_set(self):
        firewall = access.Firewall()
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("ipset", "restore"):
                raise RuntimeError("restore failed")
        firewall.run = run
        with self.assertRaises(RuntimeError):
            firewall.apply_set("caddy_allow4", "inet", [("8.8.8.8/32", 60)])
        self.assertFalse(any(call[:2] == ("ipset", "swap") for call in calls))

    def test_firewall_scopes_original_incoming_ports_and_protects_input(self):
        firewall = access.Firewall()
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            return Mock(returncode=1 if "-C" in args or ("-S" in args and args[-1] == access.CHAIN) else 0)
        firewall.run = run
        firewall.attach("iptables", "caddy_allow4", self.policy)
        jumps = [call for call in calls if "-I" in call and call[-1] == access.CHAIN]
        self.assertEqual(len(jumps), 6)
        for call in jumps:
            self.assertIn("eth0", call)
            self.assertIn("ORIGINAL", call)
            self.assertIn("--ctorigdstport", call)
            self.assertNotIn("ESTABLISHED", call)
        self.assertEqual({call[4] for call in jumps}, {"INPUT", "DOCKER-USER"})

    def test_reader_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            access.atomic_json(source, self.document)
            link = Path(directory) / "link"
            link.symlink_to(source)
            with self.assertRaises(OSError):
                access.read_json(link)

    def test_prepares_docker_user_before_daemon_start(self):
        firewall = access.Firewall()
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            return Mock(returncode=1 if "-C" in args or "-S" in args else 0)
        firewall.run = run
        firewall.attach("iptables", "caddy_allow4", self.policy)
        self.assertIn(("iptables", "-w", "5", "-N", "DOCKER-USER"), calls)
        self.assertTrue(any("-I" in call and "DOCKER-USER" in call for call in calls))

    def test_authenticated_api_queue_replace_and_revoke(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            access.atomic_json(root / "allowlist.json", self.document)
            handler_class = access.make_handler(self.policy, root, root / "status", "x" * 48)
            handler = object.__new__(handler_class)
            handler.reply = lambda code, body: (code, body)
            handler.headers = {"Authorization": "Bearer " + "x" * 48}
            handler.path = "/v1/entries"
            payload = json.dumps({"address": "8.8.8.8", "ttl_seconds": 300}).encode()
            handler.headers["Content-Length"] = str(len(payload))
            handler.rfile = io.BytesIO(payload)
            code, queued = handler.dispatch("POST")
            self.assertEqual(code, 202)
            self.assertEqual(queued["state"], "queued")
            self.assertEqual(len(access.read_json(root / "allowlist.json")["entries"]), 1)
            handler.rfile = io.BytesIO(payload)
            code, replacement = handler.dispatch("POST")
            self.assertNotEqual(queued["entry"]["id"], replacement["entry"]["id"])
            self.assertEqual(len(access.read_json(root / "allowlist.json")["entries"]), 1)
            handler.path = "/v1/entries/" + replacement["entry"]["id"]
            self.assertEqual(handler.dispatch("DELETE")[0], 202)
            self.assertEqual(access.read_json(root / "allowlist.json")["entries"], [])
            handler.path = "/v1/status"
            self.assertEqual(handler.dispatch("GET"), (200, {"state": "not_applied"}))
            handler.headers = {"Authorization": "Bearer bad"}
            self.assertEqual(handler.dispatch("GET")[0], 401)

    def test_invalid_api_request_preserves_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            access.atomic_json(root / "allowlist.json", self.document)
            handler = object.__new__(access.make_handler(self.policy, root, root / "status", "x" * 48))
            handler.reply = lambda code, body: (code, body)
            handler.path = "/v1/entries"
            handler.headers = {"Authorization": "Bearer " + "x" * 48}
            payload = b'{"address":"127.0.0.1","ttl_seconds":300}'
            handler.headers["Content-Length"] = str(len(payload))
            handler.rfile = io.BytesIO(payload)
            self.assertEqual(handler.dispatch("POST")[0], 400)
            self.assertEqual(access.read_json(root / "allowlist.json"), self.document)


if __name__ == "__main__":
    unittest.main()
