from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from .auth_service import CredentialService
from .edge import EDGE_TUNNEL_SCOPE, EdgeBindingStore
from .product_store import PostgresProductStore
from .secret_provider import LocalFileSecretProvider


class EdgeRuntimeError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_runtime(root: Path, payload: dict[str, object]) -> None:
    path = root / "edge" / "runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = {**payload, "observed_at": _utc_now()}
    fd, temp_name = tempfile.mkstemp(prefix=".runtime.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _token_file(run_root: Path, raw_token: str) -> Path:
    run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(run_root, 0o700)
    path = run_root / "cloudflared.token"
    fd, temp_name = tempfile.mkstemp(prefix=".cloudflared-token.", dir=str(run_root), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw_token)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return path


def serve_edge(*, state_root: Path | None = None, interval_seconds: float = 2.0) -> int:
    root = state_root or Path(os.environ.get("GENOS_STATE_DIR", "/var/lib/genos"))
    secret_root = Path(os.environ.get("GENOS_SECRET_DIR", str(root / "secrets")))
    binding_store = EdgeBindingStore(root)
    product_store = PostgresProductStore()
    product_store.ensure_schema()
    credentials = CredentialService(product_store, LocalFileSecretProvider(secret_root))
    run_root = Path(os.environ.get("GENOS_RUN_DIR", "/run/genos")) / "edge"
    stop_event = threading.Event()
    process: subprocess.Popen[bytes] | None = None
    active_signature: tuple[str, str] | None = None

    def _stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        while not stop_event.is_set():
            binding = binding_store.get()
            state = str(binding.get("state") or "LOCAL_ONLY")
            mode = str(binding.get("mode") or "LOCAL")
            tunnel_secret_id = str(binding.get("tunnel_secret_id") or "")
            signature = (str(binding.get("updated_at") or ""), tunnel_secret_id)
            should_run = mode == "DOMAIN" and state in {"CONFIGURED", "READY", "DEGRADED"} and bool(tunnel_secret_id)

            if process is not None and (process.poll() is not None or not should_run or signature != active_signature):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                process = None
                active_signature = None
                token_path = run_root / "cloudflared.token"
                token_path.unlink(missing_ok=True)

            if not should_run:
                _write_runtime(root, {"state": "LOCAL_ONLY" if mode == "LOCAL" else state, "process": "STOPPED", "local_core_healthy": True})
                stop_event.wait(interval_seconds)
                continue

            if process is None:
                binary = shutil.which("cloudflared")
                if not binary:
                    _write_runtime(
                        root,
                        {
                            "state": "NEEDS_ACTION",
                            "process": "STOPPED",
                            "reason": "CLOUDFLARED_NOT_INSTALLED",
                            "local_core_healthy": True,
                        },
                    )
                    stop_event.wait(interval_seconds)
                    continue
                try:
                    raw_token = credentials.get_secret_for_consumer(tunnel_secret_id, consumer=EDGE_TUNNEL_SCOPE)
                    token_path = _token_file(run_root, raw_token)
                except Exception:
                    _write_runtime(
                        root,
                        {
                            "state": "NEEDS_ACTION",
                            "process": "STOPPED",
                            "reason": "TUNNEL_SECRET_UNAVAILABLE",
                            "local_core_healthy": True,
                        },
                    )
                    stop_event.wait(interval_seconds)
                    continue
                env = os.environ.copy()
                env.pop("TUNNEL_TOKEN", None)
                process = subprocess.Popen(  # noqa: S603 - fixed executable/argv, no shell
                    [binary, "tunnel", "run", "--token-file", str(token_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    close_fds=True,
                )
                active_signature = signature
                time.sleep(0.25)
                if process.poll() is not None:
                    _write_runtime(
                        root,
                        {
                            "state": "DEGRADED",
                            "process": "EXITED",
                            "reason": "CLOUDFLARED_START_FAILED",
                            "local_core_healthy": True,
                        },
                    )
                    process = None
                    active_signature = None
                    stop_event.wait(interval_seconds)
                    continue
            _write_runtime(root, {"state": "RUNNING", "process": "RUNNING", "local_core_healthy": True})
            stop_event.wait(interval_seconds)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        (run_root / "cloudflared.token").unlink(missing_ok=True)
    return 0


def main() -> int:
    return serve_edge()


if __name__ == "__main__":
    raise SystemExit(main())
