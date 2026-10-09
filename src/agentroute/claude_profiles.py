"""Isolated Claude Code accounts, credentials, and per-account subscription usage."""

import hashlib
import json
import os
import platform
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any

from .claude_bridge import (
    KEYCHAIN_SERVICE,
    ClaudeCodeCredential,
    CredentialError,
    fetch_subscription_usage,
    read_usage_state,
    record_usage_snapshot,
    summarize_subscription_usage,
    usage_state_path,
)
from .config import AppConfig, ClaudeSubscriptionProfile

PROFILE_MODEL_MARKER = "@agentroute-profile-"
PROFILE_MODEL_SUFFIX = re.compile(
    r"^(?P<model>.+)@agentroute-profile-(?P<profile>[a-z][a-z0-9-]{0,31})$"
)


def model_for_profile(model: str, name: str) -> str:
    """Encode a subscription profile in the model override sent to the bridge."""
    return f"{model}{PROFILE_MODEL_MARKER}{name}"


def split_profile_model(model: str) -> tuple[str, str | None]:
    """Return the real Claude model and any internal subscription selector."""
    match = PROFILE_MODEL_SUFFIX.fullmatch(model)
    if not match:
        return model, None
    return match.group("model"), match.group("profile")


def profile_directory(profile: ClaudeSubscriptionProfile) -> Path:
    return Path(profile.config_dir).expanduser() if profile.config_dir else Path.home() / ".claude"


def profile_account_identity(profile: ClaudeSubscriptionProfile) -> str | None:
    """Read stable account UUIDs from Claude Code's global config, never OAuth tokens."""
    metadata_path = profile_directory(profile) / ".claude.json"
    if not profile.config_dir:
        metadata_path = Path.home() / ".claude.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    account = metadata.get("oauthAccount") if isinstance(metadata, dict) else None
    if not isinstance(account, dict):
        return None
    account_uuid = account.get("accountUuid")
    organization_uuid = account.get("organizationUuid")
    if (
        not isinstance(account_uuid, str)
        or not account_uuid
        or not isinstance(organization_uuid, str)
        or not organization_uuid
    ):
        return None
    return hashlib.sha256(f"{account_uuid}:{organization_uuid}".encode()).hexdigest()[:16]


def profile_scope(
    name: str, profile: ClaudeSubscriptionProfile, account_identity: str | None = None
) -> str:
    directory = profile_directory(profile)
    directory_hash = hashlib.sha256(str(directory).encode()).hexdigest()[:16]
    identity = account_identity or profile_account_identity(profile) or "unverified"
    return f"{name}:{profile.auth_generation}:{directory_hash}:{identity}"


def profile_usage_path(
    name: str, account_identity: str | None = None, auth_generation: str | None = None
) -> Path:
    """Keep quota history separate when a profile is signed into another account."""
    identity = account_identity or f"unverified-{auth_generation or 'unknown'}"
    return usage_state_path().parent / "claude-profiles" / name / identity / "usage.json"


class ProfileCredential(ClaudeCodeCredential):
    """Use Claude Code's own named Keychain item or private credential file.

    Claude Code 2.1 derives the Keychain suffix from the NFC configuration directory's
    SHA-256 prefix. Refreshes write back to the same store; tokens are never copied.
    """

    def __init__(self, name: str, profile: ClaudeSubscriptionProfile, **kwargs: Any) -> None:
        directory = profile_directory(profile)
        service = KEYCHAIN_SERVICE
        if profile.config_dir:
            normalized = unicodedata.normalize("NFC", str(directory))
            service += "-" + hashlib.sha256(normalized.encode()).hexdigest()[:8]
        super().__init__(keychain_service=service, **kwargs)
        self.profile_name = name
        self.account_identity = profile_account_identity(profile)
        self.profile_scope = profile_scope(name, profile, self.account_identity)
        legacy_digests = {hashlib.sha256(name.encode()).hexdigest()[:16]}
        if name == "default":
            legacy_digests.add(hashlib.sha256(b"claude-code").hexdigest()[:16])
        self.legacy_profile_digests = tuple(sorted(legacy_digests))
        self.directory = directory
        self.usage_path = profile_usage_path(name, self.account_identity, profile.auth_generation)
        self._file_store = platform.system() != "Darwin"

    def _read_keychain(self) -> dict[str, Any]:
        if platform.system() == "Darwin":
            try:
                payload = super()._read_keychain()
            except (CredentialError, ValueError):
                pass
            else:
                self._file_store = False
                return payload
        path = self.directory / ".credentials.json"
        try:
            if path.is_symlink():
                raise CredentialError("Claude credential file must not be a symlink")
            info = path.stat()
            if platform.system() != "Windows" and (
                info.st_mode & 0o077 or info.st_uid != os.getuid()
            ):
                raise CredentialError("Claude credential file must be owner-only (chmod 600)")
            if info.st_size > 1_000_000:
                raise CredentialError("Claude credential file is too large")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("expected an object")
        except (OSError, ValueError) as exc:
            raise CredentialError(
                f"Claude profile {self.profile_name} is not signed in; "
                f"run `agentroute bridge profile login {self.profile_name}`"
            ) from exc
        self._file_store = True
        return payload

    def _write_keychain(self, payload: dict[str, Any]) -> None:
        if not self._file_store:
            super()._write_keychain(payload)
            return
        target = self.directory / ".credentials.json"
        if target.is_symlink():
            raise CredentialError("Claude credential file must not be a symlink")
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(dir=self.directory, prefix=".credentials.")
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
            os.replace(temporary, target)
            temporary = None
        except OSError as exc:
            raise CredentialError("could not persist Claude profile credential") from exc
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)


def profile_credential(config: AppConfig, name: str | None = None) -> ProfileCredential:
    name = name or config.claude_subscriptions.active_profile
    profile = config.claude_subscriptions.profiles.get(name)
    if profile is None or not profile.enabled:
        raise CredentialError(f"Claude profile is unknown or disabled: {name}")
    return ProfileCredential(name, profile)


def profile_status(config: AppConfig, name: str, *, offline: bool = False) -> dict[str, Any]:
    profile = config.claude_subscriptions.profiles[name]
    account_identity = profile_account_identity(profile)
    usage_path = profile_usage_path(name, account_identity, profile.auth_generation)
    state = read_usage_state(usage_path)
    snapshot = state.get("snapshot") or {}
    rows = snapshot.get("subscription_limits") or summarize_subscription_usage(snapshot)
    status = "recorded" if rows else "unavailable"
    error = None
    if not profile.enabled:
        status = "disabled"
    elif account_identity is None:
        status = "unverified"
        error = (
            "Claude account identity is missing; sign in with "
            f"`agentroute bridge profile login {name}` before routing."
        )
    elif not offline:
        try:
            payload = fetch_subscription_usage(
                profile_credential(config, name), refresh=False, timeout=5
            )
            rows = summarize_subscription_usage(payload)
            status = "live" if rows else "unavailable"
            snapshot = {
                "captured_at": int(time.time()),
                "profile": name,
                "subscription_limits": rows,
                "windows": {},
            }
            for row in rows:
                kind = row.get("kind")
                window = (
                    "five_hour"
                    if kind in {"session", "five_hour"}
                    else "seven_day"
                    if kind in {"weekly_all", "seven_day"}
                    else None
                )
                if window:
                    snapshot["windows"][window] = {
                        "used_percent": row.get("percent"),
                        "resets_at": row.get("resets_at"),
                        "window_minutes": 300 if window == "five_hour" else 10080,
                    }
            record_usage_snapshot(snapshot, usage_path)
            state = read_usage_state(usage_path)
        except (CredentialError, OSError, ValueError) as exc:
            error = str(exc)
    return {
        "name": name,
        "active": name == config.claude_subscriptions.active_profile,
        "enabled": profile.enabled,
        "priority": profile.priority,
        "config_dir": str(profile_directory(profile)),
        "identity_status": "verified" if profile_account_identity(profile) else "unverified",
        "status": status,
        "limits": rows,
        "observed_at": state.get("updated_at"),
        "error": error,
    }
