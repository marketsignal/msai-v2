"""Secrets provider abstraction for MSAI v2.

Supports reading secrets from environment variables (default) or Azure Key Vault.
Azure Key Vault requires the optional ``azure`` dependency group::

    uv pip install msai[azure]
"""

from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SecretsProvider(Protocol):
    """Protocol for secret retrieval backends.

    Implementations must provide a ``get`` method that returns the secret value
    for a given key, or raises ``KeyError`` if the secret does not exist.
    """

    def get(self, key: str) -> str:
        """Return the secret value for *key*.

        Raises:
            KeyError: If the secret identified by *key* is not found.
        """
        ...


class EnvSecretsProvider:
    """Reads secrets from ``os.environ``.

    This is the simplest provider and suitable for local development and
    container-based deployments where secrets are injected as environment
    variables.
    """

    def get(self, key: str) -> str:
        """Return the environment variable *key*.

        Raises:
            KeyError: If the environment variable is not set.
        """
        value = os.environ.get(key)
        if value is None:
            raise KeyError(f"Environment variable not found: {key}")
        return value


class AzureKeyVaultProvider:
    """Reads secrets from Azure Key Vault.

    Uses ``azure-identity`` ``DefaultAzureCredential`` for authentication,
    which supports managed identity, CLI login, environment variables, and more.

    Dependencies (``azure-identity``, ``azure-keyvault-secrets``) are imported
    lazily so the rest of the application can run without them when Azure is not
    in use.

    Args:
        vault_url: The full URL of the Azure Key Vault,
            e.g. ``"https://my-vault.vault.azure.net"``.
    """

    def __init__(self, vault_url: str) -> None:
        self._vault_url = vault_url
        # Lazy-initialised on first call to get().
        self._client: Any = None
        self._not_found_error: type[Exception] | None = None

    def _ensure_azure(self) -> Any:
        """Import Azure SDK, create and cache the ``SecretClient`` on first use.

        Raises:
            ImportError: If the Azure SDK packages are not installed.
        """
        if self._client is not None:
            return self._client

        try:
            from azure.core.exceptions import (
                ResourceNotFoundError,
            )
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError as exc:
            raise ImportError(
                "Azure Key Vault dependencies are not installed. "
                "Install them with: uv pip install msai[azure]"
            ) from exc

        self._not_found_error = ResourceNotFoundError
        credential = DefaultAzureCredential()
        self._client = SecretClient(vault_url=self._vault_url, credential=credential)
        return self._client

    def get(self, key: str) -> str:
        """Return the secret *key* from Azure Key Vault.

        Azure Key Vault uses hyphens in secret names by convention. This method
        converts underscores to hyphens automatically so callers can use the
        same key format as environment variables
        (e.g. ``DATABASE_URL`` -> ``database-url``).

        Raises:
            KeyError: If the secret does not exist in the vault.
            ImportError: If the Azure SDK packages are not installed.
        """
        client = self._ensure_azure()
        vault_key = key.lower().replace("_", "-")

        try:
            # SecretClient.get_secret returns a KeyVaultSecret with a .value attribute.
            secret = client.get_secret(vault_key)
        except Exception as exc:
            if isinstance(exc, self._not_found_error):  # type: ignore[arg-type]
                raise KeyError(f"Secret not found in Azure Key Vault: {vault_key}") from exc
            raise

        value: str | None = secret.value
        if value is None:
            raise KeyError(f"Secret has no value in Azure Key Vault: {vault_key}")
        return value
