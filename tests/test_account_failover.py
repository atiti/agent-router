import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agentroute.capacity import CapacityState
from agentroute.config import SubscriptionProfileConfig, default_config
from agentroute.models import Tier
from agentroute.profiles import ProfileStatus, select_turn_profile


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


@pytest.mark.parametrize("work_models,age", [(None, 0), (["wrong"], 0), (["gpt-6.1-sol"], 3)])
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


@pytest.mark.parametrize(
    "usage,models,expected",
    [
        (20, ["gpt-6.1-sol"], "personal"),
        (80, ["gpt-6.1-sol"], "work"),
        (20, ["wrong"], "work"),
        (20, None, "work"),
    ],
)
def test_recovery_requires_model_support_and_quota_margin(tmp_path, usage, models, expected):
    config = setup_accounts(tmp_path, personal_models=models, work_models=["gpt-6.1-sol"])
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
