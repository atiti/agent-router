"""Explicit YOLO tier: target resolution, effort support and provider priority.

YOLO selects a model and effort only. It never changes tool permissions,
approvals, sandboxing, budgets or account access. Mappings are per backend; an
omitted mapping, or a model/effort pair the provider is not known to accept,
makes that backend unavailable for YOLO with a recorded reason. Nothing here
lowers an effort to make a target fit.
"""

from __future__ import annotations

from .claude_models import EFFORT_MODELS
from .config import AppConfig, ModelTarget
from .models import Tier

# Subscription routes tried first, in this order unless task preferences reorder
# them. Azure is the last automatic fallback.
SUBSCRIPTION_BACKENDS: tuple[str, ...] = ("gpt", "claude", "grok")
LAST_RESORT_BACKEND = "azure"

# Router-level effort spellings each provider family is known to accept. GPT and
# Azure take the Codex wire efforts up to `ultra`; the Claude bridge normalizes
# `ultra` to Anthropic `max`; Grok's highest documented effort is `xhigh`.
OPENAI_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "ultra"})
CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
GROK_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


def admitted(config: AppConfig) -> bool:
    """YOLO is admitted only when the operator raised policy.max_tier to yolo."""
    try:
        return Tier.parse(config.policy.max_tier) >= Tier.YOLO
    except ValueError:
        return False


def provider_family(config: AppConfig, backend_name: str) -> str:
    backend = config.backends.get(backend_name)
    provider = backend.codex_provider if backend else ""
    if backend_name == "gpt" or provider == "openai":
        return "openai"
    if provider.startswith("agentroute-azure"):
        return "azure"
    if provider.startswith("agentroute-claude"):
        return "claude"
    if provider.startswith("agentroute-grok"):
        return "grok"
    return "other"


def supported_efforts(config: AppConfig, backend_name: str, model: str) -> frozenset[str] | None:
    """Return the accepted efforts, or None for operator-verified other backends."""
    family = provider_family(config, backend_name)
    if family in {"openai", "azure"}:
        return OPENAI_EFFORTS
    if family == "claude":
        return CLAUDE_EFFORTS if model in EFFORT_MODELS else frozenset()
    if family == "grok":
        return GROK_EFFORTS
    return None


def resolve_target(
    config: AppConfig, backend_name: str, profile_name: str | None = None
) -> tuple[ModelTarget | None, str | None]:
    """Resolve one backend's YOLO target, or the reason it is unavailable."""
    backend = config.backends.get(backend_name)
    if backend is None:
        return None, "not_configured"
    target = backend.tiers.get(str(Tier.YOLO))
    if backend_name == "gpt" and profile_name:
        profile = config.capacity.profiles.get(profile_name)
        if profile is not None and str(Tier.YOLO) in profile.tiers:
            # Account-specific YOLO override, as for every other tier.
            target = profile.tiers[str(Tier.YOLO)]
    if target is None:
        return None, "no_yolo_mapping"
    efforts = supported_efforts(config, backend_name, target.model)
    if efforts is not None and target.reasoning_effort is not None:
        if target.reasoning_effort not in efforts:
            return None, f"unsupported_effort:{target.model}/{target.reasoning_effort}"
    return target, None


def candidate_pool(config: AppConfig) -> list[str]:
    """Subscriptions, then operator-admitted backends with a mapping, then Azure."""
    others = [
        name
        for name in config.routing.provider_selection.candidates
        if name not in SUBSCRIPTION_BACKENDS and name != LAST_RESORT_BACKEND
    ]
    return list(dict.fromkeys([*SUBSCRIPTION_BACKENDS, *others, LAST_RESORT_BACKEND]))


def priority_group(name: str) -> int:
    if name in SUBSCRIPTION_BACKENDS:
        return 0
    if name == LAST_RESORT_BACKEND:
        return 2
    return 1


def group_label(group: int) -> str:
    return ("yolo_subscription", "yolo_other", "yolo_azure")[group]
