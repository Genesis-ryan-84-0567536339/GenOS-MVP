from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any
import json
import os


GOOGLE_DRIVE_OAUTH_CLIENT_ID_ENV = "GENOS_GOOGLE_DRIVE_OAUTH_CLIENT_ID"
GOOGLE_DRIVE_OAUTH_CLIENT_SECRET_ENV = "GENOS_GOOGLE_DRIVE_OAUTH_CLIENT_SECRET"
_DISTRIBUTION_RELATIVE_PATH = Path("distribution") / "google-drive-oauth.json"
_MAX_CONFIG_BYTES = 16 * 1024
_MAX_FIELD_CHARS = 4096


class DistributionConfigError(RuntimeError):
    pass


def distribution_root() -> Path:
    """Return the immutable GenOS release root for a source/release install."""

    # /opt/genos/current/src/genos/distribution_runtime.py -> /opt/genos/current
    return Path(__file__).resolve().parents[2]


def load_google_drive_oauth_config(*, root: Path | None = None) -> dict[str, str] | None:
    """Read the publisher-owned Google OAuth application identity.

    This file belongs to the GenOS distribution, not to an end user. It may
    contain the public-client credential material required by Google's limited
    input device flow. The values must never be projected through Product API,
    logs, reports, support bundles or Product DB.
    """

    path = (root or distribution_root()) / _DISTRIBUTION_RELATIVE_PATH
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DistributionConfigError("distribution OAuth config is unreadable") from exc
    if size <= 0 or size > _MAX_CONFIG_BYTES:
        raise DistributionConfigError("distribution OAuth config has an invalid size")
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DistributionConfigError("distribution OAuth config is invalid") from exc
    if not isinstance(payload, dict):
        raise DistributionConfigError("distribution OAuth config must be an object")
    if payload.get("schema_version") != "1.0":
        raise DistributionConfigError("unsupported distribution OAuth config schema")
    if payload.get("provider") != "google-drive":
        raise DistributionConfigError("unexpected distribution OAuth provider")
    if payload.get("flow") != "limited-input-device":
        raise DistributionConfigError("unexpected distribution OAuth flow")
    client_id = _checked_field(payload.get("client_id"), "client_id")
    client_secret = _checked_field(payload.get("client_secret"), "client_secret")
    return {"client_id": client_id, "client_secret": client_secret}


def apply_distribution_environment(
    *,
    root: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> str:
    """Populate Google OAuth app identity without exposing its raw values.

    Explicit process environment is authoritative. A partial explicit override
    is never mixed with the release credential because doing so could bind a
    client id to the wrong client secret. Missing/invalid distribution config is
    non-fatal so the rest of GenOS remains healthy and Drive truthfully reports
    NOT_CONFIGURED / NEEDS_ACTION.
    """

    target = environ if environ is not None else os.environ
    has_id = bool(str(target.get(GOOGLE_DRIVE_OAUTH_CLIENT_ID_ENV, "")).strip())
    has_secret = bool(str(target.get(GOOGLE_DRIVE_OAUTH_CLIENT_SECRET_ENV, "")).strip())
    if has_id and has_secret:
        return "ENVIRONMENT"
    if has_id or has_secret:
        return "PARTIAL_ENVIRONMENT"
    try:
        config = load_google_drive_oauth_config(root=root)
    except DistributionConfigError:
        return "INVALID"
    if config is None:
        return "NOT_CONFIGURED"
    target[GOOGLE_DRIVE_OAUTH_CLIENT_ID_ENV] = config["client_id"]
    target[GOOGLE_DRIVE_OAUTH_CLIENT_SECRET_ENV] = config["client_secret"]
    return "CONFIGURED"


def _checked_field(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise DistributionConfigError(f"distribution OAuth {name} is missing")
    clean = value.strip()
    if not clean or len(clean) > _MAX_FIELD_CHARS or "\x00" in clean or "\r" in clean or "\n" in clean:
        raise DistributionConfigError(f"distribution OAuth {name} is invalid")
    return clean
