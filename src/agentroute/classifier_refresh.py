"""Use the existing catalog freshness policy for a selected configuration."""
from pathlib import Path

from .classifier import (
    JevShadowClassifier,
    OpenAICompatibleClassifier,
    catalog_age_seconds,
    is_loopback_endpoint,
)
from .config import AppConfig, save_config


def refresh_classifier_catalog(config: AppConfig, path: Path | None = None) -> None:
    settings = config.routing.classifier
    active = settings.jev_shadow if settings.engine == "jev" else settings
    if not settings.enabled or is_loopback_endpoint(active.endpoint):
        return
    age = catalog_age_seconds(active)
    if age is not None and age <= active.catalog_ttl_seconds:
        return
    classifier = (
        JevShadowClassifier(active)
        if settings.engine == "jev"
        else OpenAICompatibleClassifier(settings)
    )
    models, digest, checked_at = classifier.verify_catalog()
    active.catalog_models = models
    active.catalog_hash = digest
    active.catalog_checked_at = checked_at
    save_config(config, path)
