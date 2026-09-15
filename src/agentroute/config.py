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


class RoutingConfig(BaseModel):
    mode: Literal["heuristic"] = "heuristic"
    switching: SwitchingConfig = Field(default_factory=SwitchingConfig)


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


class AppConfig(BaseModel):
    preset: str = "balanced"
    enabled: bool = False
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    providers: dict[str, ProviderConfig]
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    ui: UIConfig = Field(default_factory=UIConfig)


def default_config() -> AppConfig:
    return AppConfig(
        providers={
            "codex": ProviderConfig(
                tiers={
                    "fast": ModelTarget(model="gpt-5.6-luna", reasoning_effort="low"),
                    "normal": ModelTarget(model="gpt-5.6-terra", reasoning_effort="medium"),
                    "smart": ModelTarget(model="gpt-5.6-sol", reasoning_effort="high"),
                    "max": ModelTarget(model="gpt-6-astra", reasoning_effort="high"),
                }
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
        }
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


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    if not path.exists():
        return default_config()
    return AppConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json", exclude_none=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path
