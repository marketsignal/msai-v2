from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from typing import Any

from msai.core.config import settings


@dataclass(slots=True, frozen=True)
class DeploymentIdentity:
    started_by: str
    strategy_id: str
    strategy_code_hash: str
    config_hash: str
    account_id: str
    paper_trading: bool
    instruments_signature: str

    def to_canonical_json(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def signature(self) -> str:
        return hashlib.sha256(self.to_canonical_json()).hexdigest()


@dataclass(slots=True, frozen=True)
class PortfolioDeploymentIdentity:
    started_by: str
    portfolio_revision_id: str
    account_id: str
    paper_trading: bool

    def to_canonical_json(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def signature(self) -> str:
        return hashlib.sha256(self.to_canonical_json()).hexdigest()


def canonicalize_user_id(user_id: str | None) -> str:
    return str(user_id or "")


def compute_instruments_signature(instruments: list[str]) -> str:
    return ",".join(sorted(str(value) for value in instruments))


def compute_config_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def derive_deployment_identity(
    *,
    user_id: str | None,
    strategy_id: str,
    strategy_code_hash: str,
    config: dict[str, Any],
    account_id: str,
    paper_trading: bool,
    instruments: list[str],
) -> DeploymentIdentity:
    return DeploymentIdentity(
        started_by=canonicalize_user_id(user_id),
        strategy_id=strategy_id,
        strategy_code_hash=strategy_code_hash,
        config_hash=compute_config_hash(config),
        account_id=account_id,
        paper_trading=paper_trading,
        instruments_signature=compute_instruments_signature(instruments),
    )


def derive_portfolio_deployment_identity(
    *,
    user_id: str | None,
    portfolio_revision_id: str,
    account_id: str,
    paper_trading: bool,
) -> PortfolioDeploymentIdentity:
    return PortfolioDeploymentIdentity(
        started_by=canonicalize_user_id(user_id),
        portfolio_revision_id=portfolio_revision_id,
        account_id=account_id,
        paper_trading=paper_trading,
    )


def generate_deployment_slug() -> str:
    return secrets.token_hex(8)


def derive_trader_id(slug: str) -> str:
    return f"{settings.nautilus_trader_id}-{slug.upper()}"


def derive_strategy_id_full(strategy_class_name: str, slug: str, order_index: int = 0) -> str:
    return f"{strategy_class_name}-{order_index}-{slug}"
