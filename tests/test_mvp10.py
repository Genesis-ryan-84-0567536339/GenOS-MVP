from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
import uuid

from genos.edge import (
    CloudflareAPI,
    EdgeBindingStore,
    EdgeConflict,
    EdgeRemoteError,
    EdgeService,
    normalize_hostname,
)


ACCOUNT = "a" * 32
ZONE = "b" * 32
API_SECRET = str(uuid.uuid4())


class FakeCredentials:
    def __init__(self) -> None:
        self.values = {API_SECRET: "raw-cloudflare-api-token-fixture"}
        self.records: dict[str, dict] = {}

    def get_secret_for_consumer(self, secret_id: str, *, consumer: str) -> str:
        if secret_id == API_SECRET:
            assert consumer == "cloudflare-edge-api"
            return self.values[secret_id]
        assert consumer == "cloudflare-edge-tunnel"
        return self.values[secret_id]

    def add(
        self,
        *,
        name: str,
        provider_name: str,
        raw_secret: str,
        consumer_scopes: list[str],
        source: str,
    ):
        secret_id = str(uuid.uuid4())
        self.values[secret_id] = raw_secret
        record = {
            "secret_id": secret_id,
            "name": name,
            "provider": provider_name,
            "active_revision": 1,
            "consumer_scopes": consumer_scopes,
        }
        self.records[secret_id] = record
        return dict(record)

    def rotate(self, secret_id: str, raw_secret: str, *, source: str):
        self.values[secret_id] = raw_secret
        current = self.records[secret_id]
        current["active_revision"] += 1
        return dict(current)


class FakeCloudflare:
    def __init__(self) -> None:
        self.tunnel_id = "11111111-2222-4333-8444-555555555555"
        self.hostname: str | None = None
        self.dns: dict[str, str] = {}
        self.fail_hostname: str | None = None
        self.calls: list[tuple[str, str]] = []

    def create_tunnel(self, *, account_id: str, name: str):
        assert account_id == ACCOUNT
        self.calls.append(("create", name))
        return {"id": self.tunnel_id, "status": "inactive"}

    def configure_tunnel(self, *, account_id: str, tunnel_id: str, hostname: str):
        assert account_id == ACCOUNT and tunnel_id == self.tunnel_id
        self.calls.append(("config", hostname))
        if hostname == self.fail_hostname:
            raise EdgeRemoteError("fixture route failure")
        self.hostname = hostname
        return {"hostname": hostname}

    def upsert_dns_cname(
        self,
        *,
        zone_id: str,
        hostname: str,
        tunnel_id: str,
        expected_record_id: str | None = None,
    ):
        assert zone_id == ZONE and tunnel_id == self.tunnel_id
        self.calls.append(("dns", hostname))
        if hostname == self.fail_hostname:
            raise EdgeRemoteError("fixture dns failure")
        existing = self.dns.get(hostname)
        if expected_record_id and existing != expected_record_id:
            raise EdgeConflict("fixture managed DNS record mismatch")
        record_id = existing or f"dns-{len(self.dns)+1}"
        self.dns[hostname] = record_id
        return {"id": record_id, "name": hostname, "content": f"{tunnel_id}.cfargotunnel.com"}

    def tunnel_token(self, *, account_id: str, tunnel_id: str):
        assert account_id == ACCOUNT and tunnel_id == self.tunnel_id
        return "raw-tunnel-token-fixture"

    def tunnel_status(self, *, account_id: str, tunnel_id: str):
        assert account_id == ACCOUNT and tunnel_id == self.tunnel_id
        return {"id": tunnel_id, "status": "HEALTHY", "config_src": "cloudflare"}


class FakeProbe:
    def verify(self, hostname: str):
        return {
            "state": "PASS",
            "hostname": hostname,
            "tls_version": "TLSv1.3",
            "certificate_present": True,
            "health_status": 200,
            "anonymous_auth_me_status": 401,
            "verified_at": "2026-08-21T00:00:00Z",
        }


class EdgeMVP10Tests(unittest.TestCase):
    def fixture(self, temp: str):
        credentials = FakeCredentials()
        cloudflare = FakeCloudflare()
        service = EdgeService(
            store=EdgeBindingStore(Path(temp)),
            credentials=credentials,  # type: ignore[arg-type]
            client_factory=lambda raw: cloudflare
            if raw == "raw-cloudflare-api-token-fixture"
            else (_ for _ in ()).throw(AssertionError("wrong token")),
            public_probe=FakeProbe(),  # type: ignore[arg-type]
        )
        return credentials, cloudflare, service

    def test_local_mode_is_first_class_without_cloudflare(self):
        with tempfile.TemporaryDirectory() as temp:
            _credentials, cloudflare, service = self.fixture(temp)
            status = service.status()
            self.assertEqual(status["state"], "LOCAL_ONLY")
            self.assertEqual(status["mode"], "LOCAL")
            self.assertTrue(status["local_core_required"])
            self.assertEqual(cloudflare.calls, [])

    def test_configure_stores_only_secret_refs_and_verify_proves_auth_protection(self):
        with tempfile.TemporaryDirectory() as temp:
            credentials, _cloudflare, service = self.fixture(temp)
            configured = service.configure(
                api_secret_id=API_SECRET,
                account_id=ACCOUNT,
                zone_id=ZONE,
                hostname="console.example.test",
            )
            self.assertEqual(configured["state"], "CONFIGURED")
            self.assertEqual(configured["mode"], "DOMAIN")
            self.assertEqual(configured["api_secret_id"], API_SECRET)
            self.assertIn(configured["tunnel_secret_id"], credentials.records)
            serialized = json.dumps(configured)
            self.assertNotIn("raw-cloudflare-api-token-fixture", serialized)
            self.assertNotIn("raw-tunnel-token-fixture", serialized)
            ready = service.verify()
            self.assertEqual(ready["state"], "READY")
            self.assertEqual(ready["public"]["anonymous_auth_me_status"], 401)
            self.assertEqual(ready["public"]["tls_version"], "TLSv1.3")

    def test_reconfigure_failure_rolls_route_back_and_preserves_local_truth(self):
        with tempfile.TemporaryDirectory() as temp:
            _credentials, cloudflare, service = self.fixture(temp)
            service.configure(
                api_secret_id=API_SECRET,
                account_id=ACCOUNT,
                zone_id=ZONE,
                hostname="old.example.test",
            )
            cloudflare.fail_hostname = "new.example.test"
            with self.assertRaises(EdgeRemoteError):
                service.configure(
                    api_secret_id=API_SECRET,
                    account_id=ACCOUNT,
                    zone_id=ZONE,
                    hostname="new.example.test",
                )
            current = service.status()
            self.assertEqual(current["hostname"], "old.example.test")
            self.assertEqual(current["last_error_code"], "RECONFIGURE_ROLLED_BACK")
            self.assertEqual(cloudflare.hostname, "old.example.test")
            self.assertIn(("config", "old.example.test"), cloudflare.calls)

    def test_disable_is_unbind_only_and_does_not_delete_remote_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            _credentials, _cloudflare, service = self.fixture(temp)
            service.configure(
                api_secret_id=API_SECRET,
                account_id=ACCOUNT,
                zone_id=ZONE,
                hostname="console.example.test",
            )
            disabled = service.disable()
            self.assertEqual(disabled["state"], "DISABLED")
            self.assertEqual(disabled["mode"], "LOCAL")
            self.assertFalse(disabled["remote_resources_deleted"])
            self.assertTrue(disabled["local_core_required"])

    def test_cloudflare_adapter_refuses_unowned_existing_cname_without_mutation(self):
        api = CloudflareAPI("fixture-api-token")
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return {
                    "success": True,
                    "result": [
                        {
                            "id": "foreign-record",
                            "name": "occupied.example.test",
                            "content": "other.example.test",
                            "comment": "not GenOS",
                        }
                    ],
                }
            raise AssertionError("foreign DNS record must not be mutated")

        api._request = request  # type: ignore[method-assign]
        with self.assertRaises(EdgeConflict):
            api.upsert_dns_cname(
                zone_id=ZONE,
                hostname="occupied.example.test",
                tunnel_id="11111111-2222-4333-8444-555555555555",
            )
        self.assertEqual([item[0] for item in calls], ["GET"])

    def test_cloudflare_adapter_recovers_matching_genos_record_after_interruption(self):
        api = CloudflareAPI("fixture-api-token")
        tunnel_id = "11111111-2222-4333-8444-555555555555"
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return {
                    "success": True,
                    "result": [
                        {
                            "id": "owned-record",
                            "name": "resume.example.test",
                            "content": f"{tunnel_id}.cfargotunnel.com",
                            "comment": "Managed by GenOS",
                        }
                    ],
                }
            if method == "PUT":
                return {
                    "success": True,
                    "result": {"id": "owned-record", "name": "resume.example.test"},
                }
            raise AssertionError("unexpected provider call")

        api._request = request  # type: ignore[method-assign]
        result = api.upsert_dns_cname(
            zone_id=ZONE,
            hostname="resume.example.test",
            tunnel_id=tunnel_id,
        )
        self.assertEqual(result["id"], "owned-record")
        self.assertEqual([item[0] for item in calls], ["GET", "PUT"])

    def test_hostname_validation_rejects_urls_and_paths(self):
        self.assertEqual(normalize_hostname("Console.Example.Test."), "console.example.test")
        for value in (
            "https://example.test",
            "example.test/path",
            "localhost",
            "-bad.example",
        ):
            with self.assertRaises(Exception):
                normalize_hostname(value)


if __name__ == "__main__":
    unittest.main()
