"""Resolve explicit models only within the selected execution source."""

import re

from .config import AppConfig, ModelTarget
from .models import Tier

MODEL_FAMILIES = frozenset({"terra", "sol", "luna", "astra", "haiku", "sonnet", "opus"})


class DirectModelError(ValueError):
    """An explicit model request cannot safely be substituted."""


def source_models(
    config: AppConfig, backend_name: str, profile_name: str | None = None
) -> dict[str, ModelTarget]:
    backend = config.backends[backend_name]
    profile = config.capacity.profiles.get(profile_name or "") if backend_name == "gpt" else None
    tiers = {
        name: profile.target(Tier.parse(name), target) if profile else target
        for name, target in backend.tiers.items()
    }
    declared = profile.models if profile else backend.models
    if any(name != target.model for name, target in declared.items()):
        raise DirectModelError("Declared model keys must match their exact model identifiers.")
    if any(target.context_window is None for target in declared.values()):
        raise DirectModelError("An additional direct model requires its source context_window.")
    return {**{target.model: target for target in tiers.values()}, **declared}


def resolve_model(
    config: AppConfig, backend_name: str, selector: str, profile_name: str | None = None
) -> ModelTarget:
    models = source_models(config, backend_name, profile_name)
    backend = config.backends[backend_name]
    profile = config.capacity.profiles.get(profile_name or "") if backend_name == "gpt" else None
    declared = profile.models if profile else backend.models
    if selector in declared and declared[selector].context_window is None:
        raise DirectModelError("An additional direct model requires its source context_window.")
    if selector in models:
        return models[selector]
    if selector.lower() in MODEL_FAMILIES:
        family = selector.lower()
        matches = [
            model
            for model in models
            if re.search(rf"(?:^|[-_/]){re.escape(family)}(?:$|[-_/])", model, re.I)
        ]
        # Configured deployment aliases take precedence over extra admitted versions.
        backend = config.backends[backend_name]
        profile = (
            config.capacity.profiles.get(profile_name or "") if backend_name == "gpt" else None
        )
        tier_models = {
            (profile.target(Tier.parse(name), target) if profile else target).model
            for name, target in backend.tiers.items()
        }
        configured = [model for model in matches if model in tier_models]
        matches = configured or matches
        if len(matches) == 1:
            return models[matches[0]]
        if matches:
            raise DirectModelError(
                f"@{selector} is ambiguous on {backend_name}: {', '.join(sorted(matches))}. "
                "Use @model:EXACT_ID."
            )
    raise DirectModelError(
        f"Model {selector!r} is not declared for source {backend_name}. "
        "Choose a model configured on that source; no fallback was requested."
    )
