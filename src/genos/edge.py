from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
import json
import os
import re
import socket
import ssl
import tempfile
import urllib.error
import urllib.request
import uuid

from .auth_service import CredentialService
from .redaction import redact


EDGE_API_SCOPE = "cloudflare-edge-api"
EDGE_TUNNEL_SCOPE = "cloudflare-edge-tunnel"
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
MISSION_CONTROL_ORIGIN = "http://127.0.0.1:17882"
MAX_RESPONSE_BYTES = 512 * 1024
_ID32 = re.compile(r"^[A-Fa-f0-9]{32}$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class EdgeError(RuntimeError):
    pass


class EdgeNeedsAction(EdgeError):
    pass


class EdgeRemoteError(EdgeError):
    pass


class EdgeConflict(EdgeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_cloudflare_id(value: str, *, field: str) -> str:
    clean = str(value or "").strip()
    if not _ID32.fullmatch(clean):
        raise EdgeError(f"invalid {field}")
    return clean.lower()


def normalize_hostname(value: str) -> str:
    candidate = str(value or "").strip().rstrip(".").lower()
    if not candidate or len(candidate) > 253 or "://" in candidate or "/" in candidate:
        raise EdgeError("invalid public hostname")
    try:
        ascii_name = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise EdgeError("invalid public hostname") from exc
    labels = ascii_name.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise EdgeError("invalid public hostname")
    return ascii_name


class EdgeBindingStore:
    """Durable non-secret edge binding metadata.

    API/tunnel tokens never enter this store. Only SecretRef IDs and provider
    object identifiers are retained so local mode and rollback remain usable
    even when Cloudflare is offline.
    """

    _ALLOWED = {
        "schema_version",
        "state",
        "mode",
        "hostname",
        "account_id",
        "zone_id",
        "api_secret_id",
        "tunnel_id",
        "tunnel_secret_id",
        "dns_record_id",
        "origin",
        "tunnel_state",
        "public_state",
        "last_verified_at",
        "last_error_code",
        "updated_at",
        "rollback",
    }

    def __init__(self, root: str | os.PathLike[str] = "/var/lib/genos") -> None:
        self.root = Path(root)
        self.path = self.root / "edge" / "binding.json"

    def get(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "schema_version": "1.0",
                "state": "LOCAL_ONLY",
                "mode": "LOCAL",
                "origin": MISSION_CONTROL_ORIGIN,
            }
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EdgeError("edge binding state is unreadable") from exc
        if not isinstance(payload, dict):
            raise EdgeError("edge binding state is invalid")
        return self._public(payload)

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        forbidden = {"token", "api_token", "tunnel_token", "secret", "client_secret", "authorization"}
        lowered = {str(key).lower() for key in payload}
        if lowered & forbidden:
            raise EdgeError("raw secret material is forbidden in edge metadata")
        unknown = set(payload) - self._ALLOWED
        if unknown:
            raise EdgeError("unsupported edge metadata key")
        normalized = self._public(payload)
        normalized["schema_version"] = "1.0"
        normalized["updated_at"] = _utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        serialized = json.dumps(redact(normalized), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=".binding.", dir=str(self.path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return normalized

    def _public(self, payload: dict[str, Any]) -> dict[str, Any]:
        return redact({key: payload.get(key) for key in self._ALLOWED if key in payload})


class CloudflareAPI:
    """Bounded typed Cloudflare v4 adapter; never logs or returns API tokens."""

    def __init__(self, api_token: str, *, origin: str = CLOUDFLARE_API) -> None:
        token = str(api_token or "").strip()
        if not token or len(token) > 4096:
            raise EdgeNeedsAction("Cloudflare API credential is invalid")
        self._token = token
        self.origin = origin.rstrip("/")

    def create_tunnel(self, *, account_id: str, name: str) -> dict[str, Any]:
        payload = self._request(
            "POST",
            f"/accounts/{account_id}/cfd_tunnel",
            {"name": name, "config_src": "cloudflare"},
        )
        result = _result_dict(payload)
        tunnel_id = str(result.get("id") or "")
        try:
            uuid.UUID(tunnel_id)
        except ValueError as exc:
            raise EdgeRemoteError("Cloudflare returned an invalid tunnel id") from exc
        return {"id": tunnel_id, "status": str(result.get("status") or "UNKNOWN")}

    def configure_tunnel(self, *, account_id: str, tunnel_id: str, hostname: str) -> dict[str, Any]:
        payload = self._request(
            "PUT",
            f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
            {
                "config": {
                    "ingress": [
                        {"hostname": hostname, "service": MISSION_CONTROL_ORIGIN},
                        {"service": "http_status:404"},
                    ]
                }
            },
        )
        _require_success(payload)
        return {"hostname": hostname, "origin": MISSION_CONTROL_ORIGIN}

    def tunnel_token(self, *, account_id: str, tunnel_id: str) -> str:
        payload = self._request("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token")
        result = payload.get("result")
        if not isinstance(result, str) or not result:
            raise EdgeRemoteError("Cloudflare tunnel token response is invalid")
        return result

    def tunnel_status(self, *, account_id: str, tunnel_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}")
        result = _result_dict(payload)
        return {
            "id": str(result.get("id") or tunnel_id),
            "status": str(result.get("status") or "UNKNOWN").upper(),
            "config_src": str(result.get("config_src") or "UNKNOWN"),
        }

    def upsert_dns_cname(self, *, zone_id: str, hostname: str, tunnel_id: str) -> dict[str, Any]:
        query = urlencode({"type": "CNAME", "name": hostname})
        listing = self._request("GET", f"/zones/{zone_id}/dns_records?{query}")
        rows = listing.get("result") if isinstance(listing.get("result"), list) else []
        body = {
            "type": "CNAME",
            "name": hostname,
            "content": f"{tunnel_id}.cfargotunnel.com",
            "ttl": 1,
            "proxied": True,
            "comment": "Managed by GenOS",
        }
        if rows:
            record_id = str(rows[0].get("id") or "") if isinstance(rows[0], dict) else ""
            if not record_id:
                raise EdgeRemoteError("Cloudflare DNS record response is invalid")
            payload = self._request("PUT", f"/zones/{zone_id}/dns_records/{record_id}", body)
        else:
            payload = self._request("POST", f"/zones/{zone_id}/dns_records", body)
        result = _result_dict(payload)
        record_id = str(result.get("id") or "")
        if not record_id:
            raise EdgeRemoteError("Cloudflare DNS mutation returned no record id")
        return {"id": record_id, "name": hostname, "content": body["content"], "proxied": True}

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not path.startswith("/"):
            raise EdgeError("invalid Cloudflare API path")
        encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.origin + path, data=encoded, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed provider origin
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            if exc.code in {400, 401, 403, 404, 409}:
                raise EdgeNeedsAction(f"Cloudflare rejected the requested edge change ({exc.code})") from exc
            raise EdgeRemoteError(f"Cloudflare API failed ({exc.code})") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EdgeRemoteError("Cloudflare API is unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise EdgeRemoteError("Cloudflare API response exceeded the safety limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EdgeRemoteError("Cloudflare API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise EdgeRemoteError("Cloudflare API returned invalid data")
        _require_success(payload)
        return payload


class PublicEdgeProbe:
    """Verify public TLS plus continued Product API Owner-session protection."""

    def verify(self, hostname: str) -> dict[str, Any]:
        clean = normalize_hostname(hostname)
        context = ssl.create_default_context()
        try:
            with socket.create_connection((clean, 443), timeout=10) as sock:
                with context.wrap_socket(sock, server_hostname=clean) as tls:
                    cert = tls.getpeercert()
                    version = tls.version() or "UNKNOWN"
            with urllib.request.urlopen(f"https://{clean}/health", timeout=15, context=context) as response:  # noqa: S310
                health_status = response.status
                health_payload = json.loads(response.read(64 * 1024).decode("utf-8"))
            protected_request = urllib.request.Request(f"https://{clean}/api/v1/auth/me", method="GET")
            protected_status = 0
            try:
                urllib.request.urlopen(protected_request, timeout=15, context=context)  # noqa: S310
            except urllib.error.HTTPError as exc:
                protected_status = exc.code
                exc.close()
        except (OSError, ssl.SSLError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise EdgeRemoteError("public DNS/TLS target is not healthy") from exc
        if health_status != 200 or not isinstance(health_payload, dict) or health_payload.get("role") != "mission-control":
            raise EdgeRemoteError("public target does not project Mission Control health")
        if protected_status != 401:
            raise EdgeRemoteError("public Product API session protection did not reject an anonymous request")
        return {
            "state": "PASS",
            "hostname": clean,
            "tls_version": version,
            "certificate_present": bool(cert),
            "health_status": health_status,
            "anonymous_auth_me_status": protected_status,
            "verified_at": _utc_now(),
        }


class EdgeService:
    def __init__(
        self,
        *,
        store: EdgeBindingStore,
        credentials: CredentialService,
        client_factory: Callable[[str], Any] = CloudflareAPI,
        public_probe: PublicEdgeProbe | None = None,
    ) -> None:
        self.store = store
        self.credentials = credentials
        self.client_factory = client_factory
        self.public_probe = public_probe or PublicEdgeProbe()

    def status(self) -> dict[str, Any]:
        current = self.store.get()
        return {**current, "local_core_required": True, "domain_optional": True}

    def configure(
        self,
        *,
        api_secret_id: str,
        account_id: str,
        zone_id: str,
        hostname: str,
        tunnel_name: str = "genos",
    ) -> dict[str, Any]:
        clean_account = normalize_cloudflare_id(account_id, field="account_id")
        clean_zone = normalize_cloudflare_id(zone_id, field="zone_id")
        clean_host = normalize_hostname(hostname)
        previous = self.store.get()
        raw_api_token = self.credentials.get_secret_for_consumer(api_secret_id, consumer=EDGE_API_SCOPE)
        client = self.client_factory(raw_api_token)
        tunnel_id = str(previous.get("tunnel_id") or "") if previous.get("api_secret_id") == api_secret_id else ""
        created = False
        if not tunnel_id:
            tunnel = client.create_tunnel(account_id=clean_account, name=_bounded_tunnel_name(tunnel_name))
            tunnel_id = str(tunnel["id"])
            created = True
        rollback = _rollback_projection(previous)
        try:
            client.configure_tunnel(account_id=clean_account, tunnel_id=tunnel_id, hostname=clean_host)
            dns = client.upsert_dns_cname(zone_id=clean_zone, hostname=clean_host, tunnel_id=tunnel_id)
            tunnel_token = client.tunnel_token(account_id=clean_account, tunnel_id=tunnel_id)
            tunnel_secret_id = self._persist_tunnel_token(previous, tunnel_id=tunnel_id, tunnel_token=tunnel_token)
        except Exception as exc:
            if previous.get("mode") == "DOMAIN" and previous.get("tunnel_id"):
                self._rollback_remote(previous, client)
                restored = dict(previous)
                restored["last_error_code"] = "RECONFIGURE_ROLLED_BACK"
                self.store.save(_filter_store(restored))
            elif created:
                self.store.save(
                    {
                        "schema_version": "1.0",
                        "state": "NEEDS_ACTION",
                        "mode": "LOCAL",
                        "origin": MISSION_CONTROL_ORIGIN,
                        "last_error_code": "EDGE_CONFIGURE_FAILED_EXTERNAL_TUNNEL_PRESERVED",
                    }
                )
            if isinstance(exc, EdgeError):
                raise
            raise EdgeRemoteError("Cloudflare edge configuration failed") from exc
        binding = {
            "schema_version": "1.0",
            "state": "CONFIGURED",
            "mode": "DOMAIN",
            "hostname": clean_host,
            "account_id": clean_account,
            "zone_id": clean_zone,
            "api_secret_id": api_secret_id,
            "tunnel_id": tunnel_id,
            "tunnel_secret_id": tunnel_secret_id,
            "dns_record_id": str(dns.get("id") or ""),
            "origin": MISSION_CONTROL_ORIGIN,
            "tunnel_state": "PENDING_RUNTIME",
            "public_state": "PENDING_VERIFY",
            "last_error_code": None,
            "rollback": rollback,
        }
        return self.store.save(binding)

    def verify(self) -> dict[str, Any]:
        current = self.store.get()
        if current.get("mode") != "DOMAIN" or not current.get("tunnel_id"):
            return {**current, "state": "LOCAL_ONLY", "verified": True, "remote_write": False}
        raw_api_token = self.credentials.get_secret_for_consumer(str(current["api_secret_id"]), consumer=EDGE_API_SCOPE)
        client = self.client_factory(raw_api_token)
        tunnel = client.tunnel_status(account_id=str(current["account_id"]), tunnel_id=str(current["tunnel_id"]))
        tunnel_state = str(tunnel.get("status") or "UNKNOWN").upper()
        if tunnel_state not in {"HEALTHY", "DEGRADED"}:
            degraded = dict(current)
            degraded.update({"state": "DEGRADED", "tunnel_state": tunnel_state, "last_error_code": "TUNNEL_NOT_HEALTHY"})
            self.store.save(_filter_store(degraded))
            raise EdgeNeedsAction("Cloudflare tunnel is not healthy yet")
        public = self.public_probe.verify(str(current["hostname"]))
        ready = dict(current)
        ready.update(
            {
                "state": "READY",
                "tunnel_state": tunnel_state,
                "public_state": str(public.get("state") or "UNKNOWN"),
                "last_verified_at": str(public.get("verified_at") or _utc_now()),
                "last_error_code": None,
            }
        )
        saved = self.store.save(_filter_store(ready))
        return {**saved, "public": public}

    def disable(self) -> dict[str, Any]:
        current = self.store.get()
        disabled = dict(current)
        disabled.update(
            {
                "state": "DISABLED",
                "mode": "LOCAL",
                "tunnel_state": "STOPPED_LOCAL_POLICY",
                "public_state": "DISABLED",
                "last_error_code": None,
            }
        )
        saved = self.store.save(_filter_store(disabled))
        return {**saved, "remote_resources_deleted": False, "local_core_required": True}

    def rollback(self) -> dict[str, Any]:
        current = self.store.get()
        rollback = current.get("rollback") if isinstance(current.get("rollback"), dict) else None
        if not rollback:
            raise EdgeConflict("no previous edge configuration is available")
        raw_api_token = self.credentials.get_secret_for_consumer(str(rollback["api_secret_id"]), consumer=EDGE_API_SCOPE)
        client = self.client_factory(raw_api_token)
        self._rollback_remote(rollback, client)
        restored = dict(rollback)
        restored["state"] = "CONFIGURED"
        restored["rollback"] = None
        restored["last_error_code"] = None
        return self.store.save(_filter_store(restored))

    def _persist_tunnel_token(self, previous: dict[str, Any], *, tunnel_id: str, tunnel_token: str) -> str:
        existing = previous.get("tunnel_secret_id") if previous.get("tunnel_id") == tunnel_id else None
        if isinstance(existing, str) and existing:
            rotated = self.credentials.rotate(existing, tunnel_token, source="cloudflare-edge")
            return str(rotated["secret_id"])
        record = self.credentials.add(
            name=f"cloudflare-tunnel-{tunnel_id}",
            provider_name="cloudflare-tunnel",
            raw_secret=tunnel_token,
            consumer_scopes=[EDGE_TUNNEL_SCOPE],
            source="cloudflare-edge",
        )
        return str(record["secret_id"])

    def _rollback_remote(self, previous: dict[str, Any], client: Any) -> None:
        if previous.get("mode") != "DOMAIN":
            return
        account_id = str(previous.get("account_id") or "")
        zone_id = str(previous.get("zone_id") or "")
        tunnel_id = str(previous.get("tunnel_id") or "")
        hostname = str(previous.get("hostname") or "")
        if not (account_id and zone_id and tunnel_id and hostname):
            return
        try:
            client.configure_tunnel(account_id=account_id, tunnel_id=tunnel_id, hostname=hostname)
            client.upsert_dns_cname(zone_id=zone_id, hostname=hostname, tunnel_id=tunnel_id)
        except Exception as exc:
            raise EdgeRemoteError("Cloudflare rollback failed; local mode remains available") from exc


def _require_success(payload: dict[str, Any]) -> None:
    if payload.get("success") is not True:
        raise EdgeNeedsAction("Cloudflare rejected the requested operation")


def _result_dict(payload: dict[str, Any]) -> dict[str, Any]:
    _require_success(payload)
    result = payload.get("result")
    if not isinstance(result, dict):
        raise EdgeRemoteError("Cloudflare returned invalid result data")
    return result


def _bounded_tunnel_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "genos").strip())[:64].strip("-.")
    return clean or "genos"


def _rollback_projection(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("mode") != "DOMAIN" or not payload.get("hostname"):
        return None
    allowed = {
        "state",
        "mode",
        "hostname",
        "account_id",
        "zone_id",
        "api_secret_id",
        "tunnel_id",
        "tunnel_secret_id",
        "dns_record_id",
        "origin",
        "tunnel_state",
        "public_state",
        "last_verified_at",
        "last_error_code",
    }
    return {key: payload.get(key) for key in allowed if key in payload}


def _filter_store(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key in EdgeBindingStore._ALLOWED}
