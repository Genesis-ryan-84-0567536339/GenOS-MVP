from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .contracts import Observation, ObservationState, SupportClass


class BoundaryMode(str, Enum):
    NATIVE = "native"
    VM = "vm"


class ProfileState(str, Enum):
    CANDIDATE = "CANDIDATE"
    VERIFIED = "VERIFIED"


@dataclass(frozen=True, slots=True)
class HostProfile:
    profile_id: str
    distribution: str
    version: str
    architecture: str
    mode: BoundaryMode
    state: ProfileState
    package_manager: str
    notes: str


# The first reference profile is deliberately CANDIDATE until the fresh-host
# workflow proves install + rerun + reboot. Candidate does not mean SUPPORTED.
REFERENCE_PROFILES: tuple[HostProfile, ...] = (
    HostProfile(
        profile_id="ubuntu-24.04-amd64-native",
        distribution="ubuntu",
        version="24.04",
        architecture="x86_64",
        mode=BoundaryMode.NATIVE,
        state=ProfileState.CANDIDATE,
        package_manager="apt",
        notes="Reference candidate; must pass real VM fresh-host E2E before support claim.",
    ),
)


@dataclass(frozen=True, slots=True)
class BoundaryDecision:
    state: str
    mode: BoundaryMode | None
    profile_id: str | None
    support_class: SupportClass
    reason: str
    requires_confirmation: bool
    mutation_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "mode": self.mode.value if self.mode else None,
            "profile_id": self.profile_id,
            "support_class": self.support_class.value,
            "reason": self.reason,
            "requires_confirmation": self.requires_confirmation,
            "mutation_allowed": self.mutation_allowed,
        }


def decide_boundary(
    observations: list[Observation],
    requested_mode: str | None,
    *,
    allow_candidate_e2e: bool = False,
) -> BoundaryDecision:
    """Resolve a boundary without guessing an irreversible host mutation.

    `allow_candidate_e2e` exists only so a controlled fresh-host acceptance lane
    can exercise a candidate profile. It does not convert that profile to
    SUPPORTED and must not be exposed as a normal user install shortcut.
    """
    by_id = {item.check_id: item for item in observations}
    platform_obs = by_id.get("platform")
    systemd_obs = by_id.get("systemd")

    if not platform_obs or not isinstance(platform_obs.observed, dict):
        return _blocked("platform UNKNOWN", SupportClass.UNSUPPORTED)
    if platform_obs.observed.get("system") != "Linux":
        return _blocked("current MVP provisioner requires Linux", SupportClass.UNSUPPORTED)
    if not systemd_obs or systemd_obs.state != ObservationState.PASS:
        return _blocked("running systemd is required before mutation", SupportClass.UNSUPPORTED)

    if requested_mode is None:
        return BoundaryDecision(
            state="NEEDS_ACTION",
            mode=None,
            profile_id=None,
            support_class=SupportClass.SUPPORTED_WITH_ACTION,
            reason="choose native for a dedicated server/VPS or vm for a shared workstation",
            requires_confirmation=True,
            mutation_allowed=False,
        )

    try:
        mode = BoundaryMode(requested_mode)
    except ValueError:
        return _blocked(f"unknown execution boundary mode: {requested_mode}", SupportClass.CONFLICT)

    if mode is BoundaryMode.VM:
        return BoundaryDecision(
            state="SUPPORTED_WITH_ACTION",
            mode=mode,
            profile_id=None,
            support_class=SupportClass.SUPPORTED_WITH_ACTION,
            reason="VM boundary is architecturally supported but no VM host provider is certified in MVP-02 yet",
            requires_confirmation=True,
            mutation_allowed=False,
        )

    distribution = str(platform_obs.observed.get("distribution") or "").lower()
    version = str(platform_obs.observed.get("distribution_version") or "")
    machine = str(platform_obs.observed.get("machine") or "")
    architecture = _normalize_arch(machine)
    profile = _match_profile(distribution, version, architecture, mode)
    if profile is None:
        return BoundaryDecision(
            state="UNSUPPORTED",
            mode=mode,
            profile_id=None,
            support_class=SupportClass.UNSUPPORTED,
            reason=f"no verified/candidate profile matches {distribution} {version} {architecture}",
            requires_confirmation=False,
            mutation_allowed=False,
        )

    if profile.state is ProfileState.VERIFIED:
        return BoundaryDecision(
            state="SUPPORTED",
            mode=mode,
            profile_id=profile.profile_id,
            support_class=SupportClass.SUPPORTED,
            reason="profile has fresh-host acceptance evidence",
            requires_confirmation=False,
            mutation_allowed=True,
        )

    if allow_candidate_e2e:
        return BoundaryDecision(
            state="CANDIDATE_E2E_ONLY",
            mode=mode,
            profile_id=profile.profile_id,
            support_class=SupportClass.SUPPORTED_WITH_ACTION,
            reason="candidate profile explicitly enabled for controlled fresh-host acceptance only",
            requires_confirmation=False,
            mutation_allowed=True,
        )

    return BoundaryDecision(
        state="SUPPORTED_WITH_ACTION",
        mode=mode,
        profile_id=profile.profile_id,
        support_class=SupportClass.SUPPORTED_WITH_ACTION,
        reason="matching profile exists but has not passed fresh-host install/rerun/reboot acceptance",
        requires_confirmation=True,
        mutation_allowed=False,
    )


def get_profile(profile_id: str) -> HostProfile:
    for profile in REFERENCE_PROFILES:
        if profile.profile_id == profile_id:
            return profile
    raise KeyError(profile_id)


def _match_profile(distribution: str, version: str, architecture: str, mode: BoundaryMode) -> HostProfile | None:
    for profile in REFERENCE_PROFILES:
        if (
            profile.distribution == distribution
            and profile.version == version
            and profile.architecture == architecture
            and profile.mode is mode
        ):
            return profile
    return None


def _normalize_arch(machine: str) -> str:
    value = machine.lower()
    if value in {"amd64", "x86_64"}:
        return "x86_64"
    if value in {"arm64", "aarch64"}:
        return "aarch64"
    return value


def _blocked(reason: str, support: SupportClass) -> BoundaryDecision:
    return BoundaryDecision(
        state=support.value,
        mode=None,
        profile_id=None,
        support_class=support,
        reason=reason,
        requires_confirmation=False,
        mutation_allowed=False,
    )
