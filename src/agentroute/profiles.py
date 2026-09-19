from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .capacity import CapacityState, subscription_state
from .config import AppConfig, SubscriptionProfileConfig, agentroute_home
from .install import _CodexAppServer


@dataclass(frozen=True)
class ProfileStatus:
    name: str
    codex_home: str
    priority: int
    enabled: bool
    authenticated: bool
    account_hash: str | None
    capacity: CapacityState
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["capacity"] = asdict(self.capacity)
        return payload


def account_hash(account_id: str | None) -> str | None:
    if not account_id:
        return None
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]


def routed_codex_binary() -> Path:
    return agentroute_home() / "bin" / "codex-bin"


def probe_profile(
    name: str,
    profile: SubscriptionProfileConfig,
    config: AppConfig,
    *,
    codex_binary: Path | None = None,
) -> ProfileStatus:
    home = Path(profile.codex_home).expanduser()
    binary = codex_binary or routed_codex_binary()
    if not profile.enabled:
        return ProfileStatus(
            name, str(home), profile.priority, False, False, None,
            CapacityState("gpt", "unavailable", "profile disabled", "profile"),
        )
    if not home.is_dir():
        return ProfileStatus(
            name, str(home), profile.priority, True, False, None,
            CapacityState("gpt", "unavailable", "CODEX_HOME does not exist", "profile"),
            "CODEX_HOME does not exist",
        )
    if not binary.is_file():
        return ProfileStatus(
            name, str(home), profile.priority, True, False, None,
            CapacityState("gpt", "unavailable", "routed Codex binary missing", "profile"),
            "routed Codex binary missing",
        )
    try:
        with _CodexAppServer(binary, codex_home=home) as client:
            account = client.request("account/read", {"refreshToken": False})
            limits = client.request(
                "account/rateLimits/read", {"supportsLunaReserve": False}
            )
        authenticated = account.get("account") is not None
        raw_account_id = limits.get("accountId") or limits.get("account_id")
        capacity = subscription_state(
            config,
            limits,
            str(raw_account_id) if raw_account_id else None,
        )
        if not authenticated:
            capacity = CapacityState(
                "gpt", "unavailable", "profile is not signed in", "profile"
            )
        return ProfileStatus(
            name,
            str(home),
            profile.priority,
            True,
            authenticated,
            account_hash(str(raw_account_id) if raw_account_id else None),
            capacity,
        )
    except (OSError, RuntimeError) as error:
        return ProfileStatus(
            name,
            str(home),
            profile.priority,
            True,
            False,
            None,
            CapacityState("gpt", "unavailable", "profile probe failed", "profile"),
            f"{type(error).__name__}: {error}",
        )


def probe_profiles(
    config: AppConfig, *, codex_binary: Path | None = None
) -> tuple[ProfileStatus, ...]:
    return tuple(
        probe_profile(name, profile, config, codex_binary=codex_binary)
        for name, profile in sorted(
            config.capacity.profiles.items(), key=lambda item: (item[1].priority, item[0])
        )
    )


def select_launch_profile(
    config: AppConfig,
    requested: str | None = None,
    *,
    codex_binary: Path | None = None,
) -> tuple[str | None, ProfileStatus | None, tuple[ProfileStatus, ...]]:
    profiles = config.capacity.profiles
    if requested:
        if requested not in profiles:
            raise ValueError(f"unknown subscription profile: {requested}")
        status = probe_profile(requested, profiles[requested], config, codex_binary=codex_binary)
        return requested, status, (status,)
    if not profiles:
        return None, None, ()
    statuses = probe_profiles(config, codex_binary=codex_binary)
    by_name = {item.name: item for item in statuses}
    active = config.capacity.active_profile
    selected = by_name.get(active) if active else None
    if not config.capacity.enabled or not config.capacity.auto_select_profile:
        return active, selected, statuses
    if selected is not None and selected.capacity.available and selected.authenticated:
        return active, selected, statuses
    candidate = next(
        (
            item
            for item in statuses
            if item.enabled and item.authenticated and item.capacity.available
        ),
        selected,
    )
    return (candidate.name if candidate else active), candidate, statuses
