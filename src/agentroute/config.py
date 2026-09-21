from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .models import Tier


class ModelTarget(BaseModel):
    model: str
    reasoning_effort: str | None = None


class ExecutionBackendConfig(BaseModel):
    """One Codex model provider plus its tier-specific model deployments."""

    enabled: bool = False
    codex_provider: str
    display_name: str
    base_url: str | None = None
    api_key_env: str | None = None
    api_key_header: str = "authorization"
    tool_compatibility: Literal["full", "functions_and_apply_patch"] | None = None
    review_model: str | None = None
    daily_budget_usd: float | None = Field(default=None, gt=0)
    monthly_budget_usd: float | None = Field(default=None, gt=0)
    fallback_backend: str | None = None
    tiers: dict[str, ModelTarget]

    def target(self, tier: Tier) -> ModelTarget:
        return self.tiers[str(tier)]


class ProviderConfig(BaseModel):
    enabled: bool = True
    tiers: dict[str, ModelTarget]

    def target(self, tier: Tier) -> ModelTarget:
        return self.tiers[str(tier)]

    def tier_for_model(self, model: str) -> Tier:
        for name, target in self.tiers.items():
            if target.model == model:
                return Tier.parse(name)
        return Tier.NORMAL


class SwitchingConfig(BaseModel):
    upgrade_confidence: float = 0.60
    downgrade_confidence: float = 0.85
    switch_penalty: float = 0.35


class ClassifierConfig(BaseModel):
    enabled: bool = False
    endpoint: str = "https://api.openai.com/v1/chat/completions"
    model: str = "gpt-5-mini"
    api_key_env: str = "AGENTROUTE_CLASSIFIER_API_KEY"
    api_key_file: str | None = None
    allow_remote: bool = False
    allow_private_http: bool = False
    timeout_seconds: float = Field(default=5.0, ge=0.1, le=30)
    ambiguity_threshold: float = Field(default=0.80, ge=0, le=1)
    max_context_chars: int = Field(default=4_000, ge=0, le=50_000)
    include_previous_assistant: bool = True
    reasoning_effort: str | None = "low"
    max_completion_tokens: int = Field(default=1_024, ge=128, le=8_192)
    catalog_ttl_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    catalog_checked_at: str | None = None
    catalog_hash: str | None = None
    catalog_models: list[str] = Field(default_factory=list)


class RoutingConfig(BaseModel):
    mode: Literal["heuristic", "hybrid", "llm"] = "hybrid"
    switching: SwitchingConfig = Field(default_factory=SwitchingConfig)
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)
    backend_by_tier: dict[str, str] = Field(
        default_factory=lambda: {
            "fast": "gpt",
            "normal": "gpt",
            "smart": "gpt",
            "max": "gpt",
        }
    )


class PolicyConfig(BaseModel):
    max_tier: str = "max"
    risk_floors: dict[str, str] = Field(
        default_factory=lambda: {
            "auth": "smart",
            "security": "smart",
            "database_migration": "smart",
            "data_deletion": "smart",
            "production": "smart",
            "cryptography": "max",
        }
    )


class AuditConfig(BaseModel):
    enabled: bool = True
    store_prompts: bool = False


class UIConfig(BaseModel):
    verbosity: Literal["silent", "compact", "verbose", "debug"] = "compact"


class SubscriptionProfileConfig(BaseModel):
    """One ChatGPT account used for turn-boundary routing.

    ``codex_home`` is retained for configurations written by AgentRoute <= 0.5.33.
    New accounts use ``credential_home`` and share the running Codex process's
    canonical ``CODEX_HOME`` for config, sessions, hooks, MCP servers, and skills.
    """

    codex_home: str | None = None
    credential_home: str | None = None
    enabled: bool = True
    priority: int = 100
    tiers: dict[str, ModelTarget] = Field(default_factory=dict)

    def target(self, tier: Tier, fallback: ModelTarget) -> ModelTarget:
        """Return a profile-compatible target, falling back to the GPT default."""
        return self.tiers.get(str(tier), fallback)


class CapacityConfig(BaseModel):
    """Opt-in subscription and API spend guardrails."""

    enabled: bool = False
    warn_percent: float = Field(default=85, ge=0, le=100)
    switch_percent: float = Field(default=95, ge=0, le=100)
    recovery_margin_percent: float = Field(default=20, ge=0, le=100)
    default_fallback_backend: str | None = None
    active_profile: str | None = None
    auto_select_profile: bool = True
    profiles: dict[str, SubscriptionProfileConfig] = Field(default_factory=dict)


class ModelPrice(BaseModel):
    input_per_million: float = Field(ge=0)
    cached_input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)
    cache_write_per_million: float | None = Field(default=None, ge=0)


class ModelCapabilities(BaseModel):
    """Operator-maintained facts used to admit models into agent workflows."""

    tool_calling: Literal["full", "apply_patch_only", "none", "unknown"] = "unknown"
    reasoning: bool | None = None
    vision: bool | None = None
    context_window: int | None = Field(default=None, ge=1)
    pricing_model: str | None = None


class CapabilityConfig(BaseModel):
    models: dict[str, ModelCapabilities] = Field(default_factory=dict)


class PricingConfig(BaseModel):
    currency: str = "USD"
    baseline_model: str = "gpt-6-astra"
    source_checked_at: str = "2026-09-17"
    models: dict[str, ModelPrice] = Field(
        default_factory=lambda: {
            "gpt-5.6-luna": ModelPrice(
                input_per_million=0.20,
                cached_input_per_million=0.02,
                output_per_million=1.20,
            ),
            "gpt-5.6-terra": ModelPrice(
                input_per_million=2.00,
                cached_input_per_million=0.20,
                output_per_million=12.00,
            ),
            "gpt-5.6-sol": ModelPrice(
                input_per_million=4.00,
                cached_input_per_million=0.40,
                output_per_million=20.00,
            ),
            "gpt-6-astra": ModelPrice(
                input_per_million=10.00,
                cached_input_per_million=1.00,
                cache_write_per_million=12.50,
                output_per_million=50.00,
            ),
            "deepseek-flash": ModelPrice(
                input_per_million=0.30,
                cached_input_per_million=0.006,
                output_per_million=1.20,
            ),
            "deepseek-v4-pro": ModelPrice(
                input_per_million=1.32,
                cached_input_per_million=0.044,
                output_per_million=3.96,
            ),
        }
    )
    aliases: dict[str, str] = Field(default_factory=dict)


class AppConfig(BaseModel):
    preset: str = "balanced"
    enabled: bool = False
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    providers: dict[str, ProviderConfig]
    backends: dict[str, ExecutionBackendConfig] = Field(default_factory=dict)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    capacity: CapacityConfig = Field(default_factory=CapacityConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    capabilities: CapabilityConfig = Field(default_factory=CapabilityConfig)


def default_config() -> AppConfig:
    gpt_tiers = {
        "fast": ModelTarget(model="gpt-5.6-luna", reasoning_effort="low"),
        "normal": ModelTarget(model="gpt-5.6-terra", reasoning_effort="medium"),
        "smart": ModelTarget(model="gpt-5.6-sol", reasoning_effort="high"),
        "max": ModelTarget(model="gpt-6-astra", reasoning_effort="high"),
    }
    config = AppConfig(
        providers={
            "codex": ProviderConfig(
                tiers=gpt_tiers
            ),
            "claude": ProviderConfig(
                enabled=False,
                tiers={
                    "fast": ModelTarget(model="haiku"),
                    "normal": ModelTarget(model="sonnet"),
                    "smart": ModelTarget(model="sonnet", reasoning_effort="high"),
                    "max": ModelTarget(model="opus"),
                },
            ),
        },
        backends={
            "gpt": ExecutionBackendConfig(
                enabled=True,
                codex_provider="openai",
                display_name="ChatGPT subscription",
                fallback_backend="azure",
                tiers=gpt_tiers,
            ),
            "azure": ExecutionBackendConfig(
                codex_provider="agentroute-azure",
                display_name="Azure OpenAI API",
                api_key_env="AZURE_OPENAI_API_KEY",
                api_key_header="api-key",
                fallback_backend="deepseek",
                tiers={
                    "fast": ModelTarget(model="gpt-5-mini", reasoning_effort="low"),
                    "normal": ModelTarget(model="gpt-5", reasoning_effort="medium"),
                    "smart": ModelTarget(model="gpt-5", reasoning_effort="high"),
                    "max": ModelTarget(model="gpt-5", reasoning_effort="high"),
                },
            ),
            "deepseek": ExecutionBackendConfig(
                codex_provider="agentroute-deepseek",
                display_name="DeepSeek API",
                base_url="https://api.deepseek.com",
                api_key_env="DEEPSEEK_API_KEY",
                tool_compatibility="functions_and_apply_patch",
                fallback_backend="gpt",
                tiers={
                    "fast": ModelTarget(model="deepseek-flash", reasoning_effort="low"),
                    "normal": ModelTarget(model="deepseek-flash", reasoning_effort="medium"),
                    "smart": ModelTarget(model="deepseek-flash", reasoning_effort="high"),
                    "max": ModelTarget(model="deepseek-flash", reasoning_effort="max"),
                },
            ),
        },
    )
    config.capabilities.models = {
        "gpt-5.6-luna": ModelCapabilities(
            tool_calling="full", reasoning=True, vision=True,
            pricing_model="gpt-5.6-luna",
        ),
        "gpt-5.6-terra": ModelCapabilities(
            tool_calling="full", reasoning=True, vision=True,
            pricing_model="gpt-5.6-terra",
        ),
        "gpt-5.6-sol": ModelCapabilities(
            tool_calling="full", reasoning=True, vision=True,
            pricing_model="gpt-5.6-sol",
        ),
        "gpt-6-astra": ModelCapabilities(
            tool_calling="full", reasoning=True, vision=True,
            pricing_model="gpt-6-astra",
        ),
        "deepseek-flash": ModelCapabilities(
            tool_calling="apply_patch_only", reasoning=True, vision=False,
            context_window=128_000, pricing_model="deepseek-flash",
        ),
        "deepseek-v4-pro": ModelCapabilities(
            tool_calling="none", reasoning=True, vision=False,
            pricing_model="deepseek-v4-pro",
        ),
    }
    config.capacity.profiles["default"] = SubscriptionProfileConfig(priority=0)
    config.capacity.active_profile = "default"
    return config


def _with_default_backends(config: AppConfig) -> AppConfig:
    """Migrate pre-backend configs without changing their established GPT mappings."""
    defaults = default_config()
    # The normal Codex credential is deliberately an implicit account.  Keep it
    # available for old installations instead of forcing users to move or copy
    # ~/.codex/auth.json before capacity routing can begin.
    config.capacity.profiles.setdefault("default", SubscriptionProfileConfig(priority=0))
    if config.capacity.active_profile is None:
        config.capacity.active_profile = "default"
    if not config.backends:
        defaults.backends["gpt"].tiers = config.providers["codex"].tiers
        config.backends = defaults.backends
    else:
        for name, backend in defaults.backends.items():
            config.backends.setdefault(name, backend)
            configured = config.backends[name]
            if configured.tool_compatibility is None:
                configured.tool_compatibility = backend.tool_compatibility
    deepseek = config.backends.get("deepseek")
    if deepseek:
        legacy_models = {"deepseek-chat", "deepseek-reasoner"}
        previous_bundled_models = {
            "fast": "deepseek-flash",
            "normal": "deepseek-flash",
            "smart": "deepseek-v4-pro",
            "max": "deepseek-v4-pro",
        }
        uses_previous_bundled_defaults = all(
            deepseek.tiers.get(tier)
            and deepseek.tiers[tier].model == model
            for tier, model in previous_bundled_models.items()
        )
        if (
            any(target.model in legacy_models for target in deepseek.tiers.values())
            or uses_previous_bundled_defaults
        ):
            deepseek.tiers = defaults.backends["deepseek"].tiers
        fast_target = deepseek.tiers.get("fast")
        if (
            fast_target
            and fast_target.model == "deepseek-flash"
            and fast_target.reasoning_effort == "none"
        ):
            fast_target.reasoning_effort = "low"
    for model, price in defaults.pricing.models.items():
        config.pricing.models.setdefault(model, price)
    for backend in config.backends.values():
        for target in backend.tiers.values():
            canonical = target.model.removeprefix("dev-")
            if target.model not in config.pricing.models and canonical in config.pricing.models:
                config.pricing.aliases.setdefault(target.model, canonical)
    for model, capabilities in defaults.capabilities.models.items():
        config.capabilities.models.setdefault(model, capabilities)
    previous_flash_price = ModelPrice(
        input_per_million=0.14,
        cached_input_per_million=0.0028,
        output_per_million=0.28,
    )
    if config.pricing.models.get("deepseek-flash") == previous_flash_price:
        config.pricing.models["deepseek-flash"] = defaults.pricing.models[
            "deepseek-flash"
        ]
        config.pricing.source_checked_at = defaults.pricing.source_checked_at
    previous_pro_price = ModelPrice(
        input_per_million=0.435,
        cached_input_per_million=0.003625,
        output_per_million=0.87,
    )
    if config.pricing.models.get("deepseek-v4-pro") == previous_pro_price:
        config.pricing.models["deepseek-v4-pro"] = defaults.pricing.models[
            "deepseek-v4-pro"
        ]
        config.pricing.source_checked_at = defaults.pricing.source_checked_at
    return config


def model_capabilities(
    config: AppConfig, backend_name: str, model: str
) -> ModelCapabilities:
    """Resolve configured model facts, falling back to conservative backend facts."""
    configured = config.capabilities.models.get(model)
    if configured is not None:
        return configured
    canonical = model.removeprefix("dev-")
    configured = config.capabilities.models.get(canonical)
    if configured is not None:
        return configured.model_copy(update={"pricing_model": canonical})
    backend = config.backends[backend_name]
    tool_calling = (
        "apply_patch_only"
        if backend.tool_compatibility == "functions_and_apply_patch"
        else "full"
        if (
            backend.tool_compatibility == "full"
            or backend_name == "gpt"
            or backend.codex_provider.startswith("agentroute-azure")
        )
        else "unknown"
    )
    return ModelCapabilities(
        tool_calling=tool_calling,
        pricing_model=model if model in config.pricing.models else None,
    )


def config_path() -> Path:
    override = os.environ.get("AGENTROUTE_CONFIG")
    if override:
        return Path(override).expanduser()
    return agentroute_home() / "config.yaml"


def data_dir() -> Path:
    override = os.environ.get("AGENTROUTE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return agentroute_home()


def agentroute_home() -> Path:
    override = os.environ.get("AGENTROUTE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agentroute"


def codex_home() -> Path:
    """Return the one canonical Codex state directory for this process."""
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    if not path.exists():
        return default_config()
    return _with_default_backends(
        AppConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    )


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json", exclude_none=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path
