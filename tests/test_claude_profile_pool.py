import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentroute.claude_bridge import (
    MAX_REASONING_ITEM_CHARS,
    REASONING_ITEM_PREFIX,
    CredentialError,
    ProfileIdentityChangedError,
    ResponsesStream,
    translate_request,
)
from agentroute.claude_profile_pool import ClaudeProfilePool
from agentroute.claude_profiles import profile_scope
from agentroute.config import ClaudeSubscriptionProfile, default_config


def _pool(monkeypatch, config, tmp_path):
    monkeypatch.setattr("agentroute.claude_profile_pool.load_config", lambda: config)

    def identity(profile):
        return f"account-{Path(profile.config_dir).name if profile.config_dir else 'default'}"

    monkeypatch.setattr("agentroute.claude_profiles.profile_account_identity", identity)
    monkeypatch.setattr("agentroute.claude_profile_pool.profile_account_identity", identity)
    monkeypatch.setattr(
        "agentroute.claude_profile_pool.ProfileCredential",
        lambda name, profile: SimpleNamespace(
            profile_name=name,
            profile_scope=profile_scope(name, profile),
            mode="claude-code",
            _read_keychain=lambda: {"claudeAiOauth": {"refreshToken": "present"}},
        ),
    )
    return ClaudeProfilePool(affinity_path=tmp_path / "affinity.json")


def test_pool_rejects_unverified_active_profile(monkeypatch, tmp_path):
    config = default_config()
    pool = _pool(monkeypatch, config, tmp_path)

    def missing_identity(_profile):
        return None

    monkeypatch.setattr("agentroute.claude_profiles.profile_account_identity", missing_identity)
    monkeypatch.setattr("agentroute.claude_profile_pool.profile_account_identity", missing_identity)

    with pytest.raises(CredentialError, match="identity is unverified"):
        pool.select({"client_metadata": {"thread_id": "unverified-thread"}}, {})


def test_pool_keeps_thread_sticky_when_active_profile_changes(monkeypatch, tmp_path):
    config = default_config()
    config.claude_subscriptions.profiles["second"] = ClaudeSubscriptionProfile(
        config_dir="/tmp/second-claude", auth_generation="login-1", priority=1
    )
    config.claude_subscriptions.active_profile = "second"
    pool = _pool(monkeypatch, config, tmp_path)
    body = {"client_metadata": {"thread_id": "thread-1"}}
    assert pool.select(body, {}).profile_name == "second"

    config.claude_subscriptions.active_profile = "default"
    restarted = _pool(monkeypatch, config, tmp_path)
    assert restarted.select(body, {}).profile_name == "second"


def test_pool_rejects_a_sticky_thread_after_profile_identity_changes(monkeypatch, tmp_path):
    config = default_config()
    config.claude_subscriptions.active_profile = "second"
    config.claude_subscriptions.profiles["second"] = ClaudeSubscriptionProfile(
        config_dir="/tmp/second-claude", auth_generation="login-1", priority=1
    )
    pool = _pool(monkeypatch, config, tmp_path)
    identities = {"/tmp/second-claude": "account-a"}
    monkeypatch.setattr(
        "agentroute.claude_profile_pool.profile_account_identity",
        lambda profile: identities.get(profile.config_dir, "default-account"),
    )
    body = {"client_metadata": {"thread_id": "thread-1"}}
    assert pool.select(body, {}).profile_name == "second"
    identities["/tmp/second-claude"] = "account-b"

    with pytest.raises(ProfileIdentityChangedError, match="start a new conversation"):
        pool.select(body, {})


def test_pool_follows_account_bound_reasoning_after_restart(monkeypatch, tmp_path):
    config = default_config()
    second = ClaudeSubscriptionProfile(
        config_dir="/tmp/second-claude", auth_generation="login-1", priority=1
    )
    config.claude_subscriptions.profiles["second"] = second
    pool = _pool(monkeypatch, config, tmp_path)
    scope = hashlib.sha256(profile_scope("second", second).encode()).hexdigest()[:16]
    encrypted = (
        REASONING_ITEM_PREFIX
        + base64.urlsafe_b64encode(
            json.dumps(
                {"profile": scope, "profile_name": "second", "prefix": "ignored", "block": {}}
            ).encode()
        ).decode()
    )

    selected = pool.select({"input": [{"type": "reasoning", "encrypted_content": encrypted}]}, {})

    assert selected.profile_name == "second"


def test_pool_rejects_reasoning_after_same_store_changes_accounts(tmp_path, monkeypatch):
    directory = tmp_path / "second"
    directory.mkdir()
    (directory / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "account-b", "organizationUuid": "org"}})
    )
    profile = ClaudeSubscriptionProfile(
        config_dir=str(directory), auth_generation="login-1", priority=1
    )
    config = default_config()
    config.claude_subscriptions.profiles["second"] = profile
    monkeypatch.setattr("agentroute.claude_profile_pool.load_config", lambda: config)
    stale_scope = hashlib.sha256(
        profile_scope("second", profile, "account-a").encode()
    ).hexdigest()[:16]
    item = {
        "type": "reasoning",
        "encrypted_content": REASONING_ITEM_PREFIX
        + base64.urlsafe_b64encode(
            json.dumps({"profile": stale_scope, "profile_name": "second"}).encode()
        ).decode(),
    }

    with pytest.raises(ProfileIdentityChangedError, match="start a new conversation"):
        ClaudeProfilePool().select({"input": [item]}, {})


@pytest.mark.parametrize("profile_state", ["removed", "disabled"])
def test_pool_rejects_reasoning_from_unavailable_profile(monkeypatch, tmp_path, profile_state):
    config = default_config()
    second = ClaudeSubscriptionProfile(
        config_dir="/tmp/second-claude", auth_generation="login-1", priority=1
    )
    if profile_state == "disabled":
        second.enabled = False
        config.claude_subscriptions.profiles["second"] = second
    pool = _pool(monkeypatch, config, tmp_path)
    scope = hashlib.sha256(profile_scope("second", second).encode()).hexdigest()[:16]
    item = {
        "type": "reasoning",
        "encrypted_content": REASONING_ITEM_PREFIX
        + base64.urlsafe_b64encode(
            json.dumps({"profile": scope, "profile_name": "second"}).encode()
        ).decode(),
    }

    with pytest.raises(
        ProfileIdentityChangedError, match="re-enable it or start a new conversation"
    ):
        pool.select({"input": [item]}, {})


def test_pool_rejects_oversized_reasoning_item(monkeypatch, tmp_path):
    config = default_config()
    pool = _pool(monkeypatch, config, tmp_path)
    item = {
        "type": "reasoning",
        "encrypted_content": REASONING_ITEM_PREFIX + "x" * MAX_REASONING_ITEM_CHARS,
    }

    with pytest.raises(ProfileIdentityChangedError, match="safe replay limit"):
        pool.select({"input": [item]}, {})


def test_multiple_thinking_blocks_replay_with_each_prior_response_item():
    request = {
        "model": "claude-haiku-5-5",
        "instructions": "Be concise.",
        "tools": [],
        "input": [{"type": "message", "role": "user", "content": "Think."}],
    }
    stream = ResponsesStream(
        "resp_multi_thought",
        request["model"],
        request_body=request,
        profile_scope="second",
        profile_name="second",
    )
    emitted = []
    for index, signature in enumerate(("sig-one", "sig-two")):
        stream.feed(
            "content_block_start",
            {
                "index": index,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
        )
        stream.feed(
            "content_block_delta",
            {"index": index, "delta": {"type": "signature_delta", "signature": signature}},
        )
        events = stream.feed("content_block_stop", {"index": index})
        emitted.append(json.loads(events[-1].split(b"data: ", 1)[1])["item"])

    continuation = {**request, "input": [*request["input"], *emitted]}
    payload, _, _ = translate_request(continuation, mode="claude-code", profile_scope="second")
    replayed = [
        block
        for message in payload["messages"]
        for block in message["content"]
        if block.get("type") == "thinking"
    ]

    assert [block["signature"] for block in replayed] == ["sig-one", "sig-two"]


def test_profile_reasoning_item_stays_below_context_item_limit():
    stream = ResponsesStream(
        "resp_bounded_thought",
        "claude-haiku-5-5",
        profile_scope="second:login-1:directory:account",
        profile_name="second",
    )
    stream.feed(
        "content_block_start",
        {"index": 0, "content_block": {"type": "thinking", "thinking": "x" * 5_000}},
    )

    events = stream.feed("content_block_stop", {"index": 0})
    item = json.loads(events[-1].split(b"data: ", 1)[1])["item"]

    assert len(item["encrypted_content"]) < 10_000
    assert len(json.dumps(item)) < 10_000


def test_oversized_thinking_block_fails_instead_of_disappearing(monkeypatch):
    monkeypatch.setattr("agentroute.claude_bridge.MAX_REASONING_BLOCK_BYTES", 8)
    stream = ResponsesStream("resp_large_thought", "claude-haiku-5-5")
    stream.feed(
        "content_block_start",
        {"index": 0, "content_block": {"type": "thinking", "thinking": "too large"}},
    )

    events = stream.feed("content_block_stop", {"index": 0})
    failure = json.loads(events[0].split(b"data: ", 1)[1])

    assert failure["type"] == "response.failed"
    assert failure["response"]["error"]["code"] == "invalid_prompt"
    assert stream.feed("message_stop", {}) == []
