from unittest.mock import Mock

from agentroute.classifier_refresh import refresh_classifier_catalog
from agentroute.config import default_config


def test_selected_configuration_refresh_is_saved_to_selected_path(tmp_path, monkeypatch):
    config = default_config()
    config.routing.classifier.enabled = True
    config.routing.classifier.catalog_checked_at = None
    classifier = Mock()
    classifier.verify_catalog.return_value = (["gpt-5.6-luna"], "digest", "timestamp")
    monkeypatch.setattr("agentroute.classifier_refresh.OpenAICompatibleClassifier",
                        lambda settings: classifier)
    saved = []
    monkeypatch.setattr("agentroute.classifier_refresh.save_config",
                        lambda value, path: saved.append((value, path)))
    selected_path = tmp_path / "selected.yaml"
    refresh_classifier_catalog(config, selected_path)
    assert saved == [(config, selected_path)]
    assert config.routing.classifier.catalog_models == ["gpt-5.6-luna"]
    assert config.routing.classifier.catalog_hash == "digest"


def test_fresh_selected_catalog_does_not_refresh_or_write(tmp_path, monkeypatch):
    config = default_config()
    config.routing.classifier.enabled = True
    monkeypatch.setattr("agentroute.classifier_refresh.catalog_age_seconds", lambda settings: 1)
    save = Mock()
    monkeypatch.setattr("agentroute.classifier_refresh.save_config", save)
    refresh_classifier_catalog(config, tmp_path / "selected.yaml")
    save.assert_not_called()
