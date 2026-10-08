"""Select and preserve Claude subscription identity for each bridge conversation."""

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .claude_bridge import (
    MAX_REASONING_ITEM_CHARS,
    REASONING_ITEM_PREFIX,
    CredentialError,
    ProfileIdentityChangedError,
    log,
    read_usage_state,
    usage_state_path,
)
from .claude_profiles import (
    ProfileCredential,
    profile_account_identity,
    profile_scope,
    profile_usage_path,
)
from .config import ClaudeSubscriptionProfile, load_config
from .context_profile import request_identity

MAX_THREAD_AFFINITY = 7_000
LEGACY_DEFAULT_PROFILE_DIGEST = hashlib.sha256(b"claude-code").hexdigest()[:16]


class ClaudeProfilePool:
    """Select a Claude account for each thread and preserve its identity."""

    mode = "claude-code"

    def __init__(self, *, affinity_path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._credentials: dict[str, tuple[str, ProfileCredential]] = {}
        self._affinity_path = (
            affinity_path or usage_state_path().parent / "claude-profile-affinity.json"
        )
        self._threads = self._load_threads()

    def _load_threads(self) -> OrderedDict[str, tuple[str, str, bool]]:
        try:
            if self._affinity_path.is_symlink() or self._affinity_path.stat().st_size > 1_000_000:
                raise CredentialError("Claude account affinity state is unsafe or too large")
            payload = json.loads(self._affinity_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return OrderedDict()
        except (OSError, ValueError) as exc:
            raise CredentialError("Claude account affinity state cannot be read safely") from exc
        if not isinstance(payload, dict) or payload.get("version") not in {1, 2}:
            raise CredentialError("Claude account affinity state has an unknown format")
        records = payload.get("threads")
        if not isinstance(records, list) or len(records) > MAX_THREAD_AFFINITY:
            raise CredentialError("Claude account affinity state is malformed")
        version = payload["version"]
        threads: OrderedDict[str, tuple[str, str, bool]] = OrderedDict()
        for record in records:
            if (
                not isinstance(record, list)
                or len(record) != (3 if version == 1 else 4)
                or not all(isinstance(value, str) and value for value in record[:3])
                or (version == 2 and not isinstance(record[3], bool))
            ):
                raise CredentialError("Claude account affinity state is malformed")
            thread_key, name, identity = record[:3]
            started = record[3] if version == 2 else True
            if started:
                threads[thread_key] = (name, identity, started)
        return threads

    def _save_threads(self) -> None:
        self._affinity_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "version": 2,
            "threads": [
                [thread_key, name, identity, started]
                for thread_key, (name, identity, started) in self._threads.items()
            ],
        }
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(
                dir=self._affinity_path.parent, prefix=".claude-profile-affinity."
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"))
            os.replace(temporary, self._affinity_path)
            temporary = None
        except OSError as exc:
            raise CredentialError(
                "Claude account affinity state could not be saved safely"
            ) from exc
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def _credential(self, name: str, profile: ClaudeSubscriptionProfile) -> ProfileCredential:
        identity = profile_account_identity(profile)
        key = (profile.config_dir or "", profile.auth_generation, identity)
        cached = self._credentials.get(name)
        if cached is None or cached[0] != repr(key):
            credential = ProfileCredential(name, profile)
            self._credentials[name] = (repr(key), credential)
        return self._credentials[name][1]

    @staticmethod
    def _recorded_percent(name: str, account_identity: str, auth_generation: str) -> float | None:
        state = read_usage_state(profile_usage_path(name, account_identity, auth_generation))
        snapshot = state.get("snapshot")
        if not isinstance(snapshot, dict):
            return None
        captured_at = snapshot.get("captured_at")
        if not isinstance(captured_at, (int, float)) or isinstance(captured_at, bool):
            return None
        try:
            captured = datetime.fromtimestamp(float(captured_at), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        sample_age = (datetime.now(timezone.utc) - captured).total_seconds()
        if sample_age < 0 or sample_age > 3600:
            return None
        windows = snapshot.get("windows")
        if not isinstance(windows, dict):
            return None
        active: list[float] = []
        for window in windows.values():
            if not isinstance(window, dict):
                continue
            used = window.get("used_percent")
            reset = window.get("resets_at")
            if not isinstance(used, (int, float)) or isinstance(used, bool) or not 0 <= used <= 100:
                continue
            if isinstance(reset, (int, float)) and reset <= time.time():
                continue
            active.append(float(used))
        return max(active) if active else None

    def select(self, body: dict[str, Any], headers: Any) -> ProfileCredential:
        config = load_config()
        profiles = config.claude_subscriptions.profiles
        eligible = [(name, profile) for name, profile in profiles.items() if profile.enabled]
        if not eligible:
            raise CredentialError("no enabled Claude subscription profiles are configured")

        known_scopes = {
            hashlib.sha256(profile_scope(name, profile).encode()).hexdigest()[:16]: name
            for name, profile in eligible
        }
        legacy_scopes = {
            hashlib.sha256(name.encode()).hexdigest()[:16]: name for name, _profile in eligible
        }
        legacy_scope_ambiguous = "claude-code" in profiles
        if legacy_scope_ambiguous:
            legacy_scopes.pop(LEGACY_DEFAULT_PROFILE_DIGEST, None)
        else:
            legacy_scopes[LEGACY_DEFAULT_PROFILE_DIGEST] = "default"
        requested = None
        for item in body.get("input") or []:
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            encoded = item.get("encrypted_content")
            if not isinstance(encoded, str) or not encoded.startswith(REASONING_ITEM_PREFIX):
                continue
            if len(encoded) > MAX_REASONING_ITEM_CHARS:
                raise ProfileIdentityChangedError(
                    "Claude reasoning item exceeds the safe replay limit; start a new conversation"
                )
            try:
                envelope = json.loads(
                    base64.urlsafe_b64decode(encoded.removeprefix(REASONING_ITEM_PREFIX))
                )
            except (ValueError, json.JSONDecodeError):
                raise ProfileIdentityChangedError(
                    "Claude reasoning item is invalid; start a new conversation"
                ) from None
            hinted_name = envelope.get("profile_name") if isinstance(envelope, dict) else None
            legacy_digest = envelope.get("profile") if isinstance(envelope, dict) else None
            legacy_name = (
                isinstance(envelope, dict)
                and not isinstance(hinted_name, str)
                and isinstance(legacy_digest, str)
                and legacy_scopes.get(legacy_digest)
            )
            if legacy_name:
                hinted_name = legacy_name
            elif not isinstance(hinted_name, str) or hinted_name not in profiles:
                if legacy_scope_ambiguous and legacy_digest == LEGACY_DEFAULT_PROFILE_DIGEST:
                    raise ProfileIdentityChangedError(
                        "legacy Claude conversation scope is ambiguous; start a new conversation"
                    )
                raise ProfileIdentityChangedError(
                    "Claude profile that produced this reasoning item is unavailable; "
                    "re-enable it or start a new conversation"
                )
            profile = profiles[hinted_name]
            if not profile.enabled:
                raise ProfileIdentityChangedError(
                    f"Claude profile {hinted_name} is disabled; "
                    "re-enable it or start a new conversation"
                )
            current_scope = hashlib.sha256(
                profile_scope(hinted_name, profile).encode()
            ).hexdigest()[:16]
            if not legacy_name and envelope.get("profile") != current_scope:
                raise ProfileIdentityChangedError(
                    f"Claude account identity changed for profile {hinted_name}; "
                    "start a new conversation"
                )
            if not legacy_name and envelope["profile"] not in known_scopes:
                raise ProfileIdentityChangedError(
                    "Claude profile that produced this reasoning item is unavailable; "
                    "re-enable it or start a new conversation"
                )
            if requested is not None and requested != hinted_name:
                raise ProfileIdentityChangedError(
                    "Claude reasoning items belong to different profiles; start a new conversation"
                )
            requested = hinted_name

        identity = request_identity(headers, body)
        raw_thread = identity.get("thread_id") or identity.get("session_id")
        thread_key = hashlib.sha256(raw_thread.encode()).hexdigest() if raw_thread else None
        with self._lock:
            sticky_state = self._threads.get(thread_key) if thread_key else None
            sticky = sticky_state[0] if sticky_state else None
            started = bool(sticky_state and sticky_state[2])
            is_continuation = requested is not None or started
            current = requested or sticky
            current = current or config.claude_subscriptions.active_profile
            selected = next(
                ((name, profile) for name, profile in eligible if name == current), None
            )
            if selected is None:
                selected = next(
                    ((name, profile) for name, profile in eligible if name == "default"),
                    eligible[0],
                )
            name, profile = selected

            current_identity = profile_account_identity(profile)
            if current_identity is None:
                raise CredentialError(
                    f"Claude account identity is unverified for profile {name}; "
                    f"run `agentroute bridge profile login {name}` before routing"
                )
            percent = self._recorded_percent(name, current_identity, profile.auth_generation)
            if (
                config.claude_subscriptions.auto_select
                and not is_continuation
                and percent is not None
                and percent >= 100
            ):
                alternatives = sorted(
                    ((candidate, item) for candidate, item in eligible if candidate != name),
                    key=lambda item: (item[1].priority, item[0]),
                )
                signed_in = [item for item in alternatives if self._signed_in(*item)]
                availability = {
                    candidate: self._recorded_percent(
                        candidate,
                        profile_account_identity(candidate_profile),
                        candidate_profile.auth_generation,
                    )
                    for candidate, candidate_profile in signed_in
                }
                available = [
                    item
                    for item in signed_in
                    if availability[item[0]] is None or availability[item[0]] < 100
                ]
                if available:
                    name, profile = available[0]
                    log(
                        f"Claude subscription {current} exhausted; "
                        f"routing new request through {name}"
                    )
                    current_identity = profile_account_identity(profile)
                    if current_identity is None:
                        raise CredentialError(
                            f"Claude account identity is unverified for profile {name}; "
                            f"run `agentroute bridge profile login {name}` before routing"
                        )
            if sticky_state and started and sticky_state[1] != current_identity:
                raise ProfileIdentityChangedError(
                    f"Claude account identity changed for profile {name}; start a new conversation"
                )
            if thread_key and sticky_state is None and len(self._threads) >= MAX_THREAD_AFFINITY:
                raise CredentialError(
                    "Claude account affinity state is full; existing thread routing is "
                    "preserved, but an unrecorded thread cannot be assigned safely"
                )
            if thread_key and sticky_state is not None:
                self._threads[thread_key] = (
                    name,
                    current_identity,
                    started or requested is not None,
                )
                self._save_threads()
            return self._credential(name, profile)

    def mark_started(self, body: dict[str, Any], headers: Any, credential: Any) -> None:
        """Persist that the upstream accepted this thread's first request."""
        identity = getattr(credential, "account_identity", None)
        name = getattr(credential, "profile_name", None)
        request = request_identity(headers, body)
        raw_thread = request.get("thread_id") or request.get("session_id")
        if not isinstance(name, str) or not isinstance(identity, str) or not raw_thread:
            return
        thread_key = hashlib.sha256(raw_thread.encode()).hexdigest()
        with self._lock:
            state = self._threads.get(thread_key)
            if state is not None and state[2]:
                if state[:2] != (name, identity):
                    raise ProfileIdentityChangedError(
                        "Claude account identity changed before the thread started; "
                        "start a new conversation"
                    )
                return
            if state is None and len(self._threads) >= MAX_THREAD_AFFINITY:
                raise CredentialError(
                    "Claude account affinity state filled before the thread started"
                )
            self._threads[thread_key] = (name, identity, True)
            self._save_threads()

    def _signed_in(self, name: str, profile: ClaudeSubscriptionProfile) -> bool:
        """Check for saved Claude Code OAuth credentials without refreshing them."""
        if profile_account_identity(profile) is None:
            return False
        try:
            payload = self._credential(name, profile)._read_keychain()
        except (CredentialError, OSError, ValueError):
            return False
        oauth = payload.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return False
        now_ms = time.time() * 1000
        refresh_token = oauth.get("refreshToken")
        refresh_expires = oauth.get("refreshTokenExpiresAt")
        if isinstance(refresh_expires, (int, float)) and refresh_expires <= now_ms:
            refresh_token = None
        access_token = oauth.get("accessToken")
        access_expires = oauth.get("expiresAt")
        access_is_live = (
            isinstance(access_token, str)
            and bool(access_token)
            and isinstance(access_expires, (int, float))
            and access_expires > now_ms
        )
        return bool(refresh_token) or access_is_live
