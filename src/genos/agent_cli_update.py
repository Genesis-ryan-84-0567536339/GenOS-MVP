from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
from typing import Any, Callable

from .redaction import redact


AGY_INSTALLER_URL = "https://antigravity.google/cli/install.sh"
AGY_UPDATE_INTERVAL_SECONDS = 6 * 60 * 60
AGY_PROVIDER_CLI = "antigravity"
AGY_BINARY_NAME = "agy"


class AgentCliUpdateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AgentCliUpdateResult:
    provider_cli: str
    installed_version: str | None
    latest_stable_version: str | None
    update_state: str
    last_check_at: str
    last_success_at: str | None
    rollback_version: str | None
    active_sha256: str | None
    installer_sha256: str | None
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_cli": self.provider_cli,
            "installed_version": self.installed_version,
            "latest_stable_version": self.latest_stable_version,
            "update_state": self.update_state,
            "last_check_at": self.last_check_at,
            "last_success_at": self.last_success_at,
            "rollback_version": self.rollback_version,
            "active_sha256": self.active_sha256,
            "installer_sha256": self.installer_sha256,
            "evidence": self.evidence,
        }


class AntigravityCliManager:
    """GenOS-owned stable channel with idle-only atomic cutover and rollback."""

    def __init__(
        self,
        root: Path | str = "/var/lib/genos/tools/antigravity-cli",
        *,
        agent_state_root: Path | str = "/var/lib/genos/agents/agy-gen",
        update_interval_seconds: int = AGY_UPDATE_INTERVAL_SECONDS,
        installer_url: str = AGY_INSTALLER_URL,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.root = Path(root)
        self.versions_root = self.root / "versions"
        self.current_link = self.root / "current"
        self.previous_link = self.root / "previous"
        self.state_path = self.root / "update-state.json"
        self.agent_state_root = Path(agent_state_root)
        self.claim_path = self.agent_state_root / "tasks" / "active-claim.json"
        self.update_interval_seconds = int(update_interval_seconds)
        self.installer_url = installer_url
        self.opener = opener

    @property
    def active_binary(self) -> Path:
        return self.current_link / AGY_BINARY_NAME

    def status(self) -> dict[str, Any]:
        persisted = self._read_state() or {}
        return redact(
            {
                "provider_cli": AGY_PROVIDER_CLI,
                "installed_version": self._active_version(),
                "latest_stable_version": persisted.get("latest_stable_version"),
                "update_state": persisted.get("update_state", "UNKNOWN"),
                "last_check_at": persisted.get("last_check_at"),
                "last_success_at": persisted.get("last_success_at"),
                "rollback_version": self._link_version(self.previous_link),
                "active_sha256": self._active_sha(),
                "installer_sha256": persisted.get("installer_sha256"),
                "evidence": persisted.get("evidence", "AGY_UPDATE_NOT_CHECKED"),
            }
        )

    def ensure_latest(
        self,
        *,
        force: bool = False,
        post_cutover_probe: Callable[[str], bool] | None = None,
    ) -> AgentCliUpdateResult:
        self._ensure_writable_layout()
        now = _utc_now()
        persisted = self._read_state() or {}
        current_version = self._active_version()
        last_success = _text(persisted.get("last_success_at"))

        if self.claim_path.exists():
            return self._save(
                AgentCliUpdateResult(
                    AGY_PROVIDER_CLI, current_version, _text(persisted.get("latest_stable_version")),
                    "UPDATE_DEFERRED_BUSY", now, last_success, self._link_version(self.previous_link),
                    self._active_sha(), _text(persisted.get("installer_sha256")),
                    "ACTIVE_WORK_CLAIM_BLOCKS_CLI_CUTOVER",
                )
            )

        if not force and self._check_is_fresh(persisted.get("last_check_at")):
            return AgentCliUpdateResult(
                AGY_PROVIDER_CLI, current_version, _text(persisted.get("latest_stable_version")),
                str(persisted.get("update_state") or "CURRENT"), str(persisted.get("last_check_at") or now),
                last_success, self._link_version(self.previous_link), self._active_sha(),
                _text(persisted.get("installer_sha256")), "UPDATE_CHECK_THROTTLED_6H",
            )

        staged: Path | None = None
        try:
            candidate = self._stage_latest()
            staged = Path(candidate["binary"])
            candidate_version = candidate["version"]
            installer_sha = candidate["installer_sha256"]
            candidate_sha = candidate["binary_sha256"]
            old_semver, new_semver = _semver(current_version), _semver(candidate_version)
            if old_semver and new_semver and new_semver < old_semver:
                return self._save(
                    AgentCliUpdateResult(
                        AGY_PROVIDER_CLI, current_version, candidate_version, "CURRENT", now, last_success,
                        self._link_version(self.previous_link), self._active_sha(), installer_sha,
                        "STABLE_CHANNEL_WOULD_DOWNGRADE_BLOCKED",
                    )
                )

            version_dir = self._store_candidate(candidate_version, staged, candidate_sha)
            if current_version == candidate_version and self.active_binary.is_file():
                return self._save(
                    AgentCliUpdateResult(
                        AGY_PROVIDER_CLI, current_version, candidate_version, "CURRENT", now, now,
                        self._link_version(self.previous_link), self._active_sha(), installer_sha,
                        "LATEST_STABLE_ALREADY_ACTIVE",
                    )
                )

            previous_target = self.current_link.resolve() if self.current_link.exists() else None
            previous_version = current_version
            self._atomic_link(self.current_link, version_dir)
            if not self._verify_active(candidate_version):
                self._restore_current(previous_target)
                return self._save(
                    AgentCliUpdateResult(
                        AGY_PROVIDER_CLI, self._active_version(), candidate_version, "ROLLED_BACK", now, last_success,
                        previous_version, self._active_sha(), installer_sha,
                        "CANDIDATE_CAPABILITY_VERIFY_FAILED_ROLLED_BACK",
                    )
                )
            if post_cutover_probe is not None and not post_cutover_probe(str(self.active_binary)):
                self._restore_current(previous_target)
                return self._save(
                    AgentCliUpdateResult(
                        AGY_PROVIDER_CLI, self._active_version(), candidate_version, "ROLLED_BACK", now, last_success,
                        previous_version, self._active_sha(), installer_sha,
                        "POST_CUTOVER_PROVIDER_PROBE_FAILED_ROLLED_BACK",
                    )
                )
            if previous_target is not None and previous_target != version_dir:
                self._atomic_link(self.previous_link, previous_target)
            return self._save(
                AgentCliUpdateResult(
                    AGY_PROVIDER_CLI, candidate_version, candidate_version,
                    "UPDATED" if previous_version else "INSTALLED", now, now, previous_version,
                    candidate_sha, installer_sha, "ANTIGRAVITY_STABLE_VERIFIED_AND_ACTIVATED",
                )
            )
        except AgentCliUpdateError as exc:
            return self._save(
                AgentCliUpdateResult(
                    AGY_PROVIDER_CLI, current_version, _text(persisted.get("latest_stable_version")), "FAILED",
                    now, last_success, self._link_version(self.previous_link), self._active_sha(),
                    _text(persisted.get("installer_sha256")), _error_evidence(exc),
                )
            )
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)

    def rollback(self) -> AgentCliUpdateResult:
        if not self.previous_link.exists():
            raise AgentCliUpdateError("NO_PREVIOUS_VERSION")
        previous_target = self.previous_link.resolve()
        current_target = self.current_link.resolve() if self.current_link.exists() else None
        self._atomic_link(self.current_link, previous_target)
        version = self._active_version()
        if not version or not self._verify_active(version):
            if current_target is not None:
                self._atomic_link(self.current_link, current_target)
            raise AgentCliUpdateError("ROLLBACK_TARGET_VERIFY_FAILED")
        if current_target is not None and current_target != previous_target:
            self._atomic_link(self.previous_link, current_target)
        now = _utc_now()
        return self._save(
            AgentCliUpdateResult(
                AGY_PROVIDER_CLI, version, None, "ROLLED_BACK", now, now,
                self._link_version(self.previous_link), self._active_sha(), None,
                "EXPLICIT_ROLLBACK_VERIFIED",
            )
        )

    def _stage_latest(self) -> dict[str, str]:
        genos_user = self._genos_user()
        persistent_candidate: Path | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="genos-agy-stage-") as temp_name:
                stage = Path(temp_name)
                os.chmod(stage, 0o755)
                os.chown(stage, genos_user.pw_uid, genos_user.pw_gid)
                installer = stage / "install.sh"
                try:
                    with self.opener(self.installer_url, timeout=60) as response, installer.open("wb") as output:
                        shutil.copyfileobj(response, output)
                except (OSError, TimeoutError) as exc:
                    raise AgentCliUpdateError("INSTALLER_DOWNLOAD_FAILED") from exc
                os.chmod(installer, 0o644)
                os.chown(installer, genos_user.pw_uid, genos_user.pw_gid)
                installer_sha = _sha256_file(installer)
                home = stage / "home"
                home.mkdir(mode=0o700)
                os.chown(home, genos_user.pw_uid, genos_user.pw_gid)
                # `genos` remains /usr/sbin/nologin at the account level. The
                # typed installer process alone gets an explicit non-login shell,
                # matching the tmux auth/runtime hardening invariant.
                self._run_as_genos(
                    ["/bin/bash", str(installer), "--skip-aliases", "--skip-path"],
                    env=self._agy_env(home), timeout=600,
                )
                installed = home / ".local" / "bin" / AGY_BINARY_NAME
                if not installed.is_file():
                    raise AgentCliUpdateError("INSTALLER_BINARY_MISSING")
                version = self._version(installed, home=home)
                if not version:
                    raise AgentCliUpdateError("STAGED_VERSION_PROBE_FAILED")
                fd, candidate_name = tempfile.mkstemp(prefix=".agy-candidate-", dir=str(self.root))
                os.close(fd)
                persistent_candidate = Path(candidate_name)
                shutil.copy2(installed, persistent_candidate)
                os.chmod(persistent_candidate, 0o755)
                return {
                    "version": version,
                    "binary": str(persistent_candidate),
                    "installer_sha256": installer_sha,
                    "binary_sha256": _sha256_file(persistent_candidate),
                }
        except Exception:
            if persistent_candidate is not None:
                persistent_candidate.unlink(missing_ok=True)
            raise

    def _store_candidate(self, version: str, candidate: Path, expected_sha: str) -> Path:
        safe = re.sub(r"[^0-9A-Za-z._+-]", "_", version)[:128]
        if not safe:
            raise AgentCliUpdateError("CANDIDATE_VERSION_INVALID")
        target_dir = self.versions_root / safe
        target = target_dir / AGY_BINARY_NAME
        if target.is_file():
            if _sha256_file(target) != expected_sha:
                raise AgentCliUpdateError("VERSION_CHECKSUM_CONFLICT")
            return target_dir
        temp_dir = self.versions_root / f".{safe}.{uuid.uuid4().hex}.tmp"
        temp_dir.mkdir(mode=0o755)
        try:
            shutil.copy2(candidate, temp_dir / AGY_BINARY_NAME)
            os.chmod(temp_dir / AGY_BINARY_NAME, 0o755)
            if _sha256_file(temp_dir / AGY_BINARY_NAME) != expected_sha:
                raise AgentCliUpdateError("STAGING_CHECKSUM_CHANGED")
            os.replace(temp_dir, target_dir)
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
        return target_dir

    def _verify_active(self, expected_version: str) -> bool:
        if not self.active_binary.is_file() or self._version(self.active_binary, home=self.agent_state_root) != expected_version:
            return False
        try:
            completed = subprocess.run(
                [str(self.active_binary), "--help"], capture_output=True, text=True, timeout=20,
                check=False, shell=False, env=self._agy_env(self.agent_state_root),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        text = f"{completed.stdout}\n{completed.stderr}"
        return completed.returncode == 0 and all(flag in text for flag in ("--model", "--effort", "--output-format"))

    def _restore_current(self, previous_target: Path | None) -> None:
        if previous_target is None:
            self.current_link.unlink(missing_ok=True)
        else:
            self._atomic_link(self.current_link, previous_target)

    def _ensure_writable_layout(self) -> None:
        try:
            self.versions_root.mkdir(parents=True, exist_ok=True, mode=0o755)
        except PermissionError as exc:
            raise AgentCliUpdateError("TOOL_ROOT_NOT_WRITABLE") from exc
        if not os.access(self.root, os.W_OK):
            raise AgentCliUpdateError("TOOL_ROOT_NOT_WRITABLE")

    def _atomic_link(self, link: Path, target: Path) -> None:
        temp = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.symlink_to(target)
            os.replace(temp, link)
        finally:
            temp.unlink(missing_ok=True)

    def _run_as_genos(self, argv: list[str], *, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
        user = self._genos_user()
        if os.geteuid() == user.pw_uid:
            command, child_env = argv, env
        elif os.geteuid() == 0:
            command = ["runuser", "-u", "genos", "--", "env", *[f"{key}={value}" for key, value in env.items()], *argv]
            child_env = {
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": env.get("LANG", "C.UTF-8"),
                "LC_ALL": env.get("LC_ALL", "C.UTF-8"),
            }
        else:
            raise AgentCliUpdateError("UPDATER_IDENTITY_INVALID")
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout, shell=False, env=child_env)
        except subprocess.TimeoutExpired as exc:
            raise AgentCliUpdateError("INSTALLER_TIMEOUT") from exc
        except OSError as exc:
            raise AgentCliUpdateError("INSTALLER_EXEC_FAILED") from exc
        if completed.returncode != 0:
            output = f"{completed.stdout}\n{completed.stderr}".lower()
            if "checksum" in output or "security halt" in output:
                code = "INSTALLER_INTEGRITY_REJECTED"
            elif "permission" in output or "denied" in output:
                code = "INSTALLER_PERMISSION_FAILED"
            elif "curl" in output or "download" in output or "network" in output:
                code = "INSTALLER_NETWORK_FAILED"
            else:
                code = f"INSTALLER_EXIT_{completed.returncode}"
            raise AgentCliUpdateError(code)
        return completed

    def _version(self, binary: Path, *, home: Path) -> str | None:
        try:
            completed = subprocess.run(
                [str(binary), "--version"], capture_output=True, text=True, check=False, timeout=20,
                shell=False, env=self._agy_env(home),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        lines = completed.stdout.strip().splitlines()
        if not lines:
            return None
        match = re.search(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", lines[0])
        return match.group(0) if match else lines[0].strip()[:128]

    def _active_version(self) -> str | None:
        return self._version(self.active_binary, home=self.agent_state_root) if self.active_binary.is_file() else None

    def _link_version(self, link: Path) -> str | None:
        if not link.exists():
            return None
        try:
            binary = link.resolve() / AGY_BINARY_NAME
        except OSError:
            return None
        return self._version(binary, home=self.agent_state_root) if binary.is_file() else None

    def _active_sha(self) -> str | None:
        return _sha256_file(self.active_binary) if self.active_binary.is_file() else None

    def _check_is_fresh(self, value: Any) -> bool:
        if not isinstance(value, str) or not value:
            return False
        try:
            observed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return False
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        return 0 <= age < self.update_interval_seconds

    def _agy_env(self, home: Path) -> dict[str, str]:
        return {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": str(home),
            "SHELL": "/bin/sh",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "NO_COLOR": "1",
            "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
        }

    @staticmethod
    def _genos_user() -> pwd.struct_passwd:
        try:
            return pwd.getpwnam("genos")
        except KeyError as exc:
            raise AgentCliUpdateError("GENOS_IDENTITY_MISSING") from exc

    def _read_state(self) -> dict[str, Any] | None:
        if not self.state_path.is_file():
            return None
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _save(self, result: AgentCliUpdateResult) -> AgentCliUpdateResult:
        self._atomic_json(
            self.state_path,
            {"schema_version": "1.0", **result.to_dict(), "source": self.installer_url, "contains_secrets": False},
        )
        return result

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(redact(payload), sort_keys=True, indent=2) + "\n", encoding="utf-8")
            os.chmod(temp, 0o644)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _error_evidence(exc: AgentCliUpdateError) -> str:
    code = re.sub(r"[^A-Z0-9_]+", "_", str(exc).upper()).strip("_")[:96] or "UNKNOWN"
    return f"UPDATE_FAILED_{code}"


def _text(value: Any) -> str | None:
    return str(value) if value not in {None, ""} else None


def _semver(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
