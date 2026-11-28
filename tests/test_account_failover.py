import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agentroute.capacity import CapacityState
from agentroute.config import SubscriptionProfileConfig, default_config
from agentroute.models import Tier
from agentroute.profiles import ProfileStatus, account_hash, select_turn_profile


def setup_accounts(tmp_path, *, personal_models, work_models, age=0):
    config = default_config()
    config.capacity.enabled = True
    config.capacity.active_profile = "personal"
    config.capacity.profiles = {}
    for name, models in [("personal", personal_models), ("work", work_models)]:
        home = tmp_path / name
        home.mkdir()
        config.capacity.profiles[name] = SubscriptionProfileConfig(credential_home=str(home))
        if models is not None:
            (home / "models_cache.json").write_text(
                json.dumps(
                    {
                        "fetched_at": (
                            datetime.now(timezone.utc) - timedelta(days=age)
                        ).isoformat(),
                        "models": [{"slug": slug} for slug in models],
                    }
                )
            )
    return config


def status(name, usage):
    return ProfileStatus(
        name,
        name,
        0,
        True,
        True,
        name,
        CapacityState(
            "gpt", "exhausted" if usage >= 95 else "healthy", "quota", "subscription", usage
        ),
    )


@pytest.mark.parametrize("work_models,age", [(None, 0), (["wrong"], 0), (["gpt-6.2-sol"], 3)])
def test_failover_skips_unknown_stale_or_incompatible_account(tmp_path, work_models, age):
    config = setup_accounts(tmp_path, personal_models=None, work_models=work_models, age=age)
    with (
        patch("agentroute.profiles.profile_name_for_home", return_value="personal"),
        patch(
            "agentroute.profiles.probe_profiles",
            return_value=(status("personal", 100), status("work", 0)),
        ),
    ):
        result = select_turn_profile(config, status("personal", 100).capacity, tier=Tier.SMART)
    assert result.selected is None


def test_failover_uses_verified_per_account_override(tmp_path):
    config = setup_accounts(tmp_path, personal_models=None, work_models=["gpt-6-luna"])
    config.capacity.profiles["work"].tiers = {"smart": config.backends["gpt"].tiers["normal"]}
    with (
        patch("agentroute.profiles.profile_name_for_home", return_value="personal"),
        patch(
            "agentroute.profiles.probe_profiles",
            return_value=(status("personal", 100), status("work", 0)),
        ),
    ):
        result = select_turn_profile(config, status("personal", 100).capacity, tier=Tier.SMART)
    assert result.selected.name == "work"
    assert result.use_profile_home


@pytest.mark.parametrize("sticky", [False, True])
def test_known_exhaustion_survives_current_probe_transport_failure(tmp_path, sticky):
    config = setup_accounts(
        tmp_path, personal_models=["gpt-6.2-sol"], work_models=["gpt-6.2-sol"]
    )
    current_id = "personal-account"
    failed = ProfileStatus(
        "personal", "personal", 0, True, False, None,
        CapacityState("gpt", "unavailable", "profile probe failed"), "timeout",
    )
    work = status("work", 20)
    with (
        patch("agentroute.profiles.profile_name_for_home", return_value="personal"),
        patch("agentroute.profiles.profile_name_for_account", return_value="personal"),
        patch("agentroute.profiles.probe_profile", return_value=failed),
        patch("agentroute.profiles.probe_profiles", return_value=(failed, work)),
    ):
        result = select_turn_profile(
            config, CapacityState("gpt", "exhausted", "rate limited"),
            current_account_id=current_id,
            sticky_profile="personal" if sticky else None,
            tier=Tier.SMART,
        )
    assert result.selected == work
    assert result.source == "failover"
    assert result.use_profile_home


def test_failed_sticky_probe_cannot_substitute_loaded_identity(tmp_path):
    config = setup_accounts(
        tmp_path, personal_models=["gpt-6.2-sol"], work_models=["gpt-6.2-sol"]
    )
    failed = ProfileStatus(
        "personal", "personal", 0, True, False, None,
        CapacityState("gpt", "unavailable", "profile probe failed"), "timeout",
    )
    work = status("work", 20)
    with (
        patch("agentroute.profiles.profile_name_for_home", return_value="work"),
        patch("agentroute.profiles.profile_name_for_account", return_value="work"),
        patch("agentroute.profiles.probe_profile", return_value=failed),
        patch("agentroute.profiles.probe_profiles", return_value=(failed, work)),
    ):
        result = select_turn_profile(
            config, CapacityState("gpt", "exhausted", "rate limited"),
            current_account_id="work-account", sticky_profile="personal", tier=Tier.SMART,
        )
    assert result.selected is None
    assert result.source == "unavailable"

def test_unknown_current_quota_fails_over_only_after_identity_matched_exhaustion(tmp_path):
    config = setup_accounts(
        tmp_path, personal_models=["gpt-6.2-sol"], work_models=["gpt-6.2-sol"]
    )
    current_id = "personal-account"
    personal = ProfileStatus(
        "personal", "personal", 0, True, True, account_hash(current_id),
        CapacityState("gpt", "exhausted", "rate limited", "subscription", 100),
    )
    work = status("work", 20)
    calls = []

    def probe(name, *args, **kwargs):
        calls.append((name, kwargs.get("timeout")))
        return {"personal": personal, "work": work}[name]

    with (
        patch("agentroute.profiles.profile_name_for_home", return_value="personal"),
        patch("agentroute.profiles.profile_name_for_account", return_value="personal"),
        patch("agentroute.profiles.probe_profile", side_effect=probe),
    ):
        result = select_turn_profile(
            config, CapacityState("gpt", "unknown", "no snapshot"),
            current_account_id=current_id, tier=Tier.SMART,
        )
    assert result.selected == work
    assert result.current == personal
    assert result.source == "failover"
    assert calls == [("personal", 2.0), ("work", None)]


@pytest.mark.parametrize("mode", ["unknown", "failed", "mismatch", "missing_hash", "healthy"])
def test_unknown_quota_probe_never_switches_on_unverified_evidence(tmp_path, mode):
    config = setup_accounts(
        tmp_path, personal_models=["gpt-6.2-sol"], work_models=["gpt-6.2-sol"]
    )
    current_id = "personal-account"
    current_hash = account_hash(current_id)
    probe_status = "exhausted" if mode in {"mismatch", "missing_hash"} else mode
    if mode == "failed":
        probe_status = "unavailable"
    observed = ProfileStatus(
        "personal", "personal", 0, True, mode != "failed",
        "other-account" if mode == "mismatch" else None if mode == "missing_hash"
        else current_hash,
        CapacityState("gpt", probe_status, "probe result"),
    )
    with (
        patch("agentroute.profiles.profile_name_for_home", return_value="personal"),
        patch("agentroute.profiles.profile_name_for_account", return_value="personal"),
        patch("agentroute.profiles.probe_profile", return_value=observed) as probe,
    ):
        result = select_turn_profile(
            config, CapacityState("gpt", "unknown", "no snapshot"),
            current_account_id=current_id, tier=Tier.SMART,
        )
    assert result.selected.name == "personal"
    assert result.source == "account_match"
    assert not result.switched
    probe.assert_called_once()


def test_unknown_quota_without_account_identity_does_not_probe_or_switch(tmp_path):
    config = setup_accounts(tmp_path, personal_models=None, work_models=["gpt-6.2-sol"])
    with (
        patch("agentroute.profiles.profile_name_for_home", return_value="personal"),
        patch("agentroute.profiles.probe_profile") as probe,
    ):
        result = select_turn_profile(config, CapacityState("gpt", "unknown", "no snapshot"))
    assert result.selected.name == "personal"
    assert not result.switched
    probe.assert_not_called()


def test_unknown_quota_exhaustion_does_not_select_unsupported_destination(tmp_path):
    config = setup_accounts(tmp_path, personal_models=["gpt-6.2-sol"], work_models=["wrong"])
    current_id = "personal-account"
    personal = ProfileStatus(
        "personal", "personal", 0, True, True, account_hash(current_id),
        CapacityState("gpt", "exhausted", "rate limited"),
    )
    with (
        patch("agentroute.profiles.profile_name_for_home", return_value="personal"),
        patch("agentroute.profiles.profile_name_for_account", return_value="personal"),
        patch(
            "agentroute.profiles.probe_profile",
            side_effect=lambda name, *a, **kw: personal if name == "personal" else status(name, 0),
        ),
    ):
        result = select_turn_profile(
            config, CapacityState("gpt", "unknown", "no snapshot"),
            current_account_id=current_id, tier=Tier.SMART,
        )
    assert result.selected is None
    assert result.source == "unavailable"


@pytest.mark.parametrize("destination_hash", [None, "same"])
def test_unknown_quota_exhaustion_requires_distinct_account_identity(tmp_path, destination_hash):
    config = setup_accounts(
        tmp_path, personal_models=["gpt-6.2-sol"], work_models=["gpt-6.2-sol"]
    )
    current_id = "personal-account"
    current_hash = account_hash(current_id)
    personal = ProfileStatus(
        "personal", "personal", 0, True, True, current_hash,
        CapacityState("gpt", "exhausted", "rate limited"),
    )
    duplicate = ProfileStatus(
        "work", "work", 0, True, True, current_hash if destination_hash else None,
        CapacityState("gpt", "healthy", "apparently healthy"),
    )
    with (
        patch("agentroute.profiles.profile_name_for_home", return_value="personal"),
        patch("agentroute.profiles.profile_name_for_account", return_value="personal"),
        patch(
            "agentroute.profiles.probe_profile",
            side_effect=lambda name, *a, **kw: personal if name == "personal" else duplicate,
        ),
    ):
        result = select_turn_profile(
            config, CapacityState("gpt", "unknown", "no snapshot"),
            current_account_id=current_id, tier=Tier.SMART,
        )
    assert result.selected is None


def test_sticky_profile_keeps_affinity_when_quota_probe_fails(tmp_path):
    config = setup_accounts(
        tmp_path, personal_models=["gpt-6.2-sol"], work_models=["gpt-6.2-sol"]
    )
    config.capacity.active_profile = "work"
    failed = ProfileStatus(
        "personal", "personal", 0, True, False, None,
        CapacityState("gpt", "unavailable", "profile probe failed"), "timeout",
    )
    work = status("work", 90)  # Not below the recovery margin.
    with (
        patch(
            "agentroute.profiles.profile_account_hash",
            return_value=account_hash("personal-account"),
        ),
        patch(
            "agentroute.profiles.probe_profile",
            side_effect=lambda name, *a, **kw: failed if name == "personal" else work,
        ),
    ):
        result = select_turn_profile(
            config, CapacityState("gpt", "unknown", "no snapshot"),
            sticky_profile="personal", current_account_id="personal-account", tier=Tier.SMART,
        )
    assert result.selected.name == "personal"
    assert result.source == "session_affinity"
    assert result.selected.capacity.status == "unknown"
    assert not result.use_profile_home


def test_failed_sticky_probe_never_claims_other_loaded_account(tmp_path):
    config = setup_accounts(
        tmp_path, personal_models=["gpt-6.2-sol"], work_models=["gpt-6.2-sol"]
    )
    config.capacity.active_profile = "work"
    failed = ProfileStatus(
        "personal", "personal", 0, True, False, None,
        CapacityState("gpt", "unavailable", "profile probe failed"), "timeout",
    )
    work = status("work", 90)
    with (
        patch(
            "agentroute.profiles.profile_account_hash", return_value=account_hash("other-account")
        ),
        patch(
            "agentroute.profiles.probe_profile",
            side_effect=lambda name, *a, **kw: failed if name == "personal" else work,
        ),
    ):
        result = select_turn_profile(
            config, CapacityState("gpt", "unknown", "no snapshot"),
            sticky_profile="personal", current_account_id="personal-account", tier=Tier.SMART,
        )
    assert result.selected is None
    assert result.source == "unavailable"


def test_failed_sticky_probe_does_not_run_known_unsupported_model(tmp_path):
    config = setup_accounts(tmp_path, personal_models=["wrong"], work_models=["gpt-6.2-sol"])
    config.capacity.active_profile = "work"
    failed = ProfileStatus(
        "personal", "personal", 0, True, False, None,
        CapacityState("gpt", "unavailable", "profile probe failed"), "timeout",
    )
    work = status("work", 90)
    with (
        patch(
            "agentroute.profiles.profile_account_hash",
            return_value=account_hash("personal-account"),
        ),
        patch(
            "agentroute.profiles.probe_profile",
            side_effect=lambda name, *a, **kw: failed if name == "personal" else work,
        ),
    ):
        result = select_turn_profile(
            config, CapacityState("gpt", "unknown", "no snapshot"),
            sticky_profile="personal", current_account_id="personal-account", tier=Tier.SMART,
        )
    assert result.selected is None
    assert result.source == "unavailable"


@pytest.mark.parametrize("enabled,automatic", [(False, True), (True, False)])
def test_unknown_quota_probe_respects_disabled_capacity(tmp_path, enabled, automatic):
    config = setup_accounts(tmp_path, personal_models=None, work_models=None)
    config.capacity.enabled = enabled
    config.capacity.auto_select_profile = automatic
    with (
        patch("agentroute.profiles.profile_name_for_home", return_value="personal"),
        patch("agentroute.profiles.probe_profile") as probe,
    ):
        result = select_turn_profile(
            config, CapacityState("gpt", "unknown", "no snapshot"),
            current_account_id="personal-account",
        )
    assert result.selected.name == "personal"
    probe.assert_not_called()


@pytest.mark.parametrize(
    "usage,models,expected",
    [
        (20, ["gpt-6.2-sol"], "personal"),
        (80, ["gpt-6.2-sol"], "work"),
        (20, ["wrong"], "work"),
        (20, None, "work"),
    ],
)
def test_recovery_requires_model_support_and_quota_margin(tmp_path, usage, models, expected):
    config = setup_accounts(tmp_path, personal_models=models, work_models=["gpt-6.2-sol"])
    with patch(
        "agentroute.profiles.probe_profile",
        side_effect=lambda name, *a, **kw: status(name, usage if name == "personal" else 30),
    ):
        result = select_turn_profile(
            config, status("work", 30).capacity, sticky_profile="work", tier=Tier.SMART
        )
    assert result.selected.name == expected
    assert result.source == (
        "preferred_account_recovered" if expected == "personal" else "session_affinity"
    )

@pytest.mark.parametrize('enabled,automatic', [(False, True), (True, False)])
def test_disabled_capacity_cannot_run_known_unsupported_model(tmp_path, enabled, automatic):
    config = setup_accounts(tmp_path, personal_models=['gpt-6-luna'], work_models=None)
    config.capacity.enabled = enabled
    config.capacity.auto_select_profile = automatic
    with patch('agentroute.profiles.profile_name_for_home', return_value='personal'):
        result = select_turn_profile(config, status('personal', 10).capacity, tier=Tier.SMART)
    assert result.selected is None


def test_catalog_refresh_uses_native_cache_and_isolates_generated_aliases(tmp_path):
    from pathlib import Path

    from agentroute.profiles import profile_catalog_slugs, refresh_profile_catalog

    config = setup_accounts(tmp_path, personal_models=None, work_models=None)
    (tmp_path / 'personal' / 'auth.json').write_text('{}')

    class NativeClient:
        def __init__(self, binary, *, codex_home):
            self.home = codex_home
            assert (self.home / 'auth.json').is_symlink()
            assert not (self.home / 'config.toml').exists()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, params):
            assert method == 'model/list'
            (self.home / 'models_cache.json').write_text(json.dumps({
                'fetched_at': datetime.now(timezone.utc).isoformat(),
                'models': [{'slug': 'supported-native-model'}],
            }))
            return {'data': [{'model': 'generated-alias-must-not-be-trusted'}]}

    with patch('agentroute.profiles._CodexAppServer', NativeClient):
        refresh_profile_catalog(config, 'personal')
    assert profile_catalog_slugs('personal', config.capacity.profiles['personal']) == {
        'supported-native-model'
    }
    assert Path(tmp_path / 'personal' / 'models_cache.json').stat().st_mode & 0o077 == 0
