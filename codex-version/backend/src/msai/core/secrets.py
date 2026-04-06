from __future__ import annotations

import os
from functools import lru_cache
from typing import Protocol

from msai.core.config import settings


class SecretsProvider(Protocol):
    def get(self, key: str) -> str: ...


class EnvSecretsProvider:
    """Development provider that reads directly from environment variables."""

    def get(self, key: str) -> str:
        value = os.environ.get(key)
        if value is None:
            raise KeyError(f"Secret '{key}' not found in environment")
        return value


class AzureKeyVaultProvider:
    """Production provider backed by Azure Key Vault."""

    def __init__(self, vault_url: str) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        self._client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())

    def get(self, key: str) -> str:
        value = self._client.get_secret(key).value
        if value is None:
            raise KeyError(f"Secret '{key}' missing in Azure Key Vault")
        return value


@lru_cache
def get_secrets_provider() -> SecretsProvider:
    if settings.environment == "production" and settings.azure_key_vault_url:
        return AzureKeyVaultProvider(settings.azure_key_vault_url)
    return EnvSecretsProvider()
