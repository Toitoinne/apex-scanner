"""Chargement de la configuration (YAML) et des secrets (.env).

Deux niveaux :
- config.yaml : paramètres de base, versionnés.
- surcharges runtime : appliquées par les boucles 2/3 sur une LISTE BLANCHE de
  clés (jamais la sécurité ni l'ingestion), journalisées et réversibles.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("APEX_CONFIG", ROOT / "config" / "config.yaml"))
SAFETY_PATH = Path(os.environ.get("APEX_SAFETY", ROOT / "config" / "safety.yaml"))

# Préfixes que les boucles automatiques ont le droit de surcharger.
MUTABLE_PREFIXES = (
    "models.error_weights",
    "models.positive_class_weight",
    "models.champion_window",
    "models.lgbm_window",
    "calibration.",
    "bandit.thresholds",
    "bandit.discount",
)
# Préfixes explicitement interdits (garde-fou section 13).
FORBIDDEN_PREFIXES = ("ingestion", "safety", "labels", "supervision", "chain")


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    helius_api_key: str = ""
    helius_rpc_url: str = ""
    helius_ws_url: str = ""
    # websockets gratuits (séparés par des virgules) pour le flux complet pump.fun ;
    # plusieurs URL = redondance (déduplication par signature)
    solana_ws_urls: str = "wss://api.mainnet-beta.solana.com"
    pumpportal_api_key: str = ""
    dune_api_key: str = ""
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql://apex:apex@localhost:5432/apex"
    data_dir: str = str(ROOT / "data")
    dashboard_token: str = ""
    # portefeuille DÉDIÉ au trading réel (clé privée base58). Vide = trading réel impossible.
    wallet_private_key: str = ""

    def rpc_url(self) -> str:
        if self.helius_rpc_url:
            return self.helius_rpc_url
        return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"

    def ws_url(self) -> str:
        if self.helius_ws_url:
            return self.helius_ws_url
        return f"wss://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"


def _get(d: dict, dotted: str) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        cur = cur[part]
    return cur


def _set(d: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def is_mutable(key: str) -> bool:
    if key.startswith(FORBIDDEN_PREFIXES):
        return False
    return key.startswith(MUTABLE_PREFIXES)


@dataclass
class Config:
    base: dict
    safety: dict
    overrides: dict

    @classmethod
    def load(cls, path: Path = CONFIG_PATH, safety_path: Path = SAFETY_PATH) -> "Config":
        with open(path, encoding="utf-8") as f:
            base = yaml.safe_load(f)
        with open(safety_path, encoding="utf-8") as f:
            safety = yaml.safe_load(f)
        return cls(base=base, safety=safety, overrides={})

    @property
    def data(self) -> dict:
        merged = copy.deepcopy(self.base)
        for k, v in self.overrides.items():
            _set(merged, k, v)
        return merged

    def get(self, dotted: str, default: Any = None) -> Any:
        try:
            return _get(self.data, dotted)
        except (KeyError, TypeError):
            return default

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def set_override(self, key: str, value: Any) -> None:
        if not is_mutable(key):
            raise PermissionError(f"clé de configuration non modifiable automatiquement : {key}")
        self.overrides[key] = value

    def clear_override(self, key: str) -> None:
        self.overrides.pop(key, None)

    def version(self) -> str:
        blob = json.dumps({"o": self.overrides, "b": self.base}, sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()[:10]


_secrets: Secrets | None = None


def secrets() -> Secrets:
    global _secrets
    if _secrets is None:
        _secrets = Secrets()
    return _secrets
