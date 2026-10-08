"""Select and preserve Claude subscription identity for each bridge conversation."""

import base64
import hashlib
import json
import os
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .claude_bridge import (
    MAX_REASONING_ITEM_CHARS,
    REASONING_ITEM_PREFIX,
    CredentialError,
    ProfileIdentityChangedError,
    usage_state_path,
)
from .claude_profiles import ProfileCredential, profile_account_identity, profile_scope
from .config import ClaudeSubscriptionProfile, load_config
from .context_profile import request_identity


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

    def _load_threads(self) -> OrderedDict[str, tuple[str, str]]:
        try:
            if self._affinity_path.is_symlink() or self._affinity_path.stat().st_size > 1_000_000:
                raise CredentialError("Claude account affinity state is unsafe or too large")
            payload = json.loads(self._affinity_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return OrderedDict()
        except (OSError, ValueError) as exc:
            raise CredentialError("Claude account affinity state cannot be read safely") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise CredentialError("Claude account affinity state has an unknown format")
        records = payload.get("threads")
        if not isinstance(records, list):
            raise CredentialError("Claude account affinity state is malformed")
        threads: OrderedDict[str, tuple[str, str]] = OrderedDict()
        for record in records[-2048:]:
            if (
                not isinstance(record, list)
                or len(record) != 3
                or not all(isinstance(value, str) and value for value in record)
            ):
                raise CredentialError("Claude account affinity state is malformed")
            thread_key, name, identity = record
            threads[thread_key] = (name, identity)
        return threads

    def _save_threads(self) -> None:
        self._affinity_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "version": 1,
            "threads": [
                [thread_key, name, identity]
                for thread_key, (name, identity) in self._threads.items()
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
            if not isinstance(hinted_name, str) or hinted_name not in profiles:
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
            if envelope.get("profile") != current_scope:
                raise ProfileIdentityChangedError(
                    f"Claude account identity changed for profile {hinted_name}; "
                    "start a new conversation"
                )
            if envelope["profile"] not in known_scopes:
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
            if sticky_state and sticky_state[1] != current_identity:
                raise ProfileIdentityChangedError(
                    f"Claude account identity changed for profile {name}; start a new conversation"
                )
            if thread_key:
                self._threads[thread_key] = (name, current_identity)
                self._threads.move_to_end(thread_key)
                while len(self._threads) > 2048:
                    self._threads.popitem(last=False)
                self._save_threads()
            return self._credential(name, profile)
