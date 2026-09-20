from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .capacity import CapacityState, subscription_state
from .config import AppConfig, SubscriptionProfileConfig, agentroute_home
from .install import _CodexAppServer

PROFILE_SHARED_FILES = ("config.toml", "hooks.json", "AGENTS.md")
PROFILE_SHARED_DIRECTORIES = ("skills", "rules")


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
        capacity = asdict(self.capacity)
        capacity.pop("account_id", None)
        payload["capacity"] = capacity
        return payload


@dataclass(frozen=True)
class TurnProfileSelection:
    """A ChatGPT subscription profile selected for one routed turn."""

    current_name: str | None
    current: ProfileStatus | None
    selected: ProfileStatus | None
    switched: bool
    source: str
    use_profile_home: bool


def account_hash(account_id: str | None) -> str | None:
    if not account_id:
        return None
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]


def profile_account_hash(profile: SubscriptionProfileConfig) -> str | None:
    """Read a profile's local account identity without exposing the raw identifier."""
    auth_path = Path(profile.codex_home).expanduser() / "auth.json"
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return None
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None
    raw_account_id = tokens.get("account_id") or tokens.get("accountId")
    return account_hash(str(raw_account_id)) if raw_account_id else None


def profile_name_for_account(config: AppConfig, account_id: str | None) -> str | None:
    """Match a hook account to a configured profile using hashes only."""
    current_hash = account_hash(account_id)
    if current_hash is None:
        return None
    matches = [
        (profile.priority, name)
        for name, profile in config.capacity.profiles.items()
        if profile.enabled and profile_account_hash(profile) == current_hash
    ]
    return min(matches)[1] if matches else None


def profile_name_for_home(config: AppConfig, codex_home: str | None = None) -> str | None:
    """Identify the profile that owns the current process's CODEX_HOME."""
    raw_home = codex_home or os.environ.get("CODEX_HOME")
    if not raw_home:
        return None
    current_home = Path(raw_home).expanduser().resolve()
    matches = [
        (profile.priority, name)
        for name, profile in config.capacity.profiles.items()
        if profile.enabled and Path(profile.codex_home).expanduser().resolve() == current_home
    ]
    return min(matches)[1] if matches else None


def _current_profile_status(
    name: str,
    profile: SubscriptionProfileConfig,
    capacity: CapacityState,
    current_hash: str | None,
) -> ProfileStatus:
    return ProfileStatus(
        name=name,
        codex_home=str(Path(profile.codex_home).expanduser()),
        priority=profile.priority,
        enabled=profile.enabled,
        authenticated=current_hash is not None,
        account_hash=current_hash,
        capacity=capacity,
    )


def routed_codex_binary() -> Path:
    return agentroute_home() / "bin" / "codex-bin"


def bootstrap_profile_home(source: Path, destination: Path) -> tuple[str, ...]:
    """Copy reusable Codex setup into a profile without copying account state."""
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if source == destination:
        raise ValueError("profile source and destination must be different directories")
    if not source.is_dir():
        raise ValueError(f"profile source does not exist: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    copied: list[str] = []
    for name in PROFILE_SHARED_FILES:
        origin = source / name
        target = destination / name
        if not origin.is_file():
            continue
        if target.exists():
            shutil.copy2(target, target.with_name(f"{name}.agentroute-profile-backup-{timestamp}"))
        shutil.copy2(origin, target)
        copied.append(name)
    for name in PROFILE_SHARED_DIRECTORIES:
        origin = source / name
        target = destination / name
        if not origin.is_dir():
            continue
        shutil.copytree(origin, target, dirs_exist_ok=True)
        copied.append(name)
    return tuple(copied)


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


def select_turn_profile(
    config: AppConfig,
    current_capacity: CapacityState,
    *,
    current_account_id: str | None = None,
    sticky_profile: str | None = None,
    codex_binary: Path | None = None,
) -> TurnProfileSelection | None:
    """Select an authenticated profile without switching on unknown telemetry.

    A previously selected profile remains sticky while its telemetry is healthy,
    warning, or unknown. A new failover only occurs when the current profile is
    authoritatively exhausted or unavailable and another profile has known capacity.
    """
    profiles = config.capacity.profiles
    if not profiles:
        return None

    current_hash = account_hash(current_account_id)
    matched_name = profile_name_for_home(config) or profile_name_for_account(
        config, current_account_id
    )
    if not config.capacity.enabled or not config.capacity.auto_select_profile:
        if matched_name is None:
            return None
        current = _current_profile_status(
            matched_name,
            profiles[matched_name],
            current_capacity,
            current_hash,
        )
        return TurnProfileSelection(
            matched_name,
            current,
            current,
            False,
            "account_match",
            False,
        )
    current_name = sticky_profile or matched_name

    if sticky_profile is not None:
        profile = profiles.get(sticky_profile)
        current = (
            probe_profile(
                sticky_profile,
                profile,
                config,
                codex_binary=codex_binary,
            )
            if profile is not None
            else None
        )
        if (
            current is not None
            and current.enabled
            and current.authenticated
            and current.capacity.status not in {"exhausted", "unavailable"}
        ):
            return TurnProfileSelection(
                current_name,
                current,
                current,
                False,
                "session_affinity",
                current.account_hash != current_hash,
            )
        needs_failover = True
    elif current_capacity.status not in {"exhausted", "unavailable"}:
        if matched_name is None:
            return None
        current = _current_profile_status(
            matched_name,
            profiles[matched_name],
            current_capacity,
            current_hash,
        )
        return TurnProfileSelection(
            matched_name,
            current,
            current,
            False,
            "account_match",
            False,
        )
    else:
        current = None
        needs_failover = True

    if not needs_failover:
        return None

    statuses = probe_profiles(config, codex_binary=codex_binary)
    by_name = {item.name: item for item in statuses}
    if current_name is None and current_hash is not None:
        current_name = next(
            (item.name for item in statuses if item.account_hash == current_hash),
            None,
        )
    if current_name is None:
        current_name = config.capacity.active_profile
    current = current or by_name.get(current_name)
    current_profile_hash = current.account_hash if current is not None else current_hash

    candidate = next(
        (
            item
            for item in statuses
            if item.name != current_name
            and (
                current_profile_hash is None
                or item.account_hash is None
                or item.account_hash != current_profile_hash
            )
            and item.enabled
            and item.authenticated
            and item.capacity.status in {"healthy", "warning"}
        ),
        None,
    )
    if candidate is None:
        return TurnProfileSelection(current_name, current, None, False, "unavailable", False)
    return TurnProfileSelection(
        current_name,
        by_name.get(current_name),
        candidate,
        candidate.name != current_name,
        "failover",
        candidate.account_hash != current_hash,
    )
