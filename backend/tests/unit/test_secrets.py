"""Tests for the secrets provider abstraction."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from msai.core.secrets import (
    AzureKeyVaultProvider,
    EnvSecretsProvider,
    SecretsProvider,
)

# ---------------------------------------------------------------------------
# Helpers — shared Azure mock setup
# ---------------------------------------------------------------------------

# A real exception subclass that the ``except`` clause inside get() can catch.
_FakeResourceNotFoundError: type[Exception] = type("ResourceNotFoundError", (Exception,), {})


def _azure_modules(
    *,
    client_cls: MagicMock,
    credential_cls: MagicMock,
) -> dict[str, MagicMock]:
    """Return a ``sys.modules`` patch dict that fakes the Azure SDK."""
    return {
        "azure": MagicMock(),
        "azure.identity": MagicMock(DefaultAzureCredential=credential_cls),
        "azure.keyvault": MagicMock(),
        "azure.keyvault.secrets": MagicMock(SecretClient=client_cls),
        "azure.core": MagicMock(),
        "azure.core.exceptions": MagicMock(
            ResourceNotFoundError=_FakeResourceNotFoundError,
        ),
    }


# ---------------------------------------------------------------------------
# EnvSecretsProvider
# ---------------------------------------------------------------------------


class TestEnvSecretsProvider:
    """Tests for EnvSecretsProvider."""

    def test_env_secrets_provider_returns_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set an env var and verify get() returns it."""
        # Arrange
        monkeypatch.setenv("MSAI_TEST_SECRET", "super-secret-value")
        provider = EnvSecretsProvider()

        # Act
        result = provider.get("MSAI_TEST_SECRET")

        # Assert
        assert result == "super-secret-value"

    def test_env_secrets_provider_raises_on_missing(self) -> None:
        """Verify KeyError is raised when the env var does not exist."""
        # Arrange
        provider = EnvSecretsProvider()
        key = "MSAI_DEFINITELY_DOES_NOT_EXIST_12345"
        os.environ.pop(key, None)

        # Act & Assert
        with pytest.raises(KeyError, match="Environment variable not found"):
            provider.get(key)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestSecretsProviderProtocol:
    """Tests that concrete classes satisfy the SecretsProvider Protocol."""

    def test_env_secrets_provider_satisfies_protocol(self) -> None:
        """EnvSecretsProvider must be a structural subtype of SecretsProvider."""
        # Arrange
        provider = EnvSecretsProvider()

        # Assert
        assert isinstance(provider, SecretsProvider)

    def test_azure_key_vault_provider_satisfies_protocol(self) -> None:
        """AzureKeyVaultProvider must be a structural subtype of SecretsProvider."""
        # Arrange
        provider = AzureKeyVaultProvider(vault_url="https://fake.vault.azure.net")

        # Assert
        assert isinstance(provider, SecretsProvider)


# ---------------------------------------------------------------------------
# AzureKeyVaultProvider
# ---------------------------------------------------------------------------


class TestAzureKeyVaultProvider:
    """Tests for AzureKeyVaultProvider (Azure SDK is mocked)."""

    def test_get_returns_secret_value(self) -> None:
        """Verify get() returns the secret value from Key Vault."""
        # Arrange
        mock_secret = MagicMock()
        mock_secret.value = "vault-secret-123"

        mock_client_instance = MagicMock()
        mock_client_instance.get_secret.return_value = mock_secret
        mock_client_cls = MagicMock(return_value=mock_client_instance)
        mock_credential_cls = MagicMock()

        provider = AzureKeyVaultProvider(vault_url="https://my-vault.vault.azure.net")

        modules = _azure_modules(client_cls=mock_client_cls, credential_cls=mock_credential_cls)
        with patch.dict("sys.modules", modules):
            # Act
            result = provider.get("DATABASE_URL")

        # Assert
        assert result == "vault-secret-123"
        mock_client_instance.get_secret.assert_called_once_with("database-url")

    def test_get_converts_underscores_to_hyphens(self) -> None:
        """Verify that key names are normalised: MY_API_SECRET -> my-api-secret."""
        # Arrange
        mock_secret = MagicMock()
        mock_secret.value = "value"

        mock_client_instance = MagicMock()
        mock_client_instance.get_secret.return_value = mock_secret
        mock_client_cls = MagicMock(return_value=mock_client_instance)
        mock_credential_cls = MagicMock()

        provider = AzureKeyVaultProvider(vault_url="https://my-vault.vault.azure.net")

        modules = _azure_modules(client_cls=mock_client_cls, credential_cls=mock_credential_cls)
        with patch.dict("sys.modules", modules):
            # Act
            provider.get("MY_API_SECRET")

        # Assert
        mock_client_instance.get_secret.assert_called_once_with("my-api-secret")

    def test_get_raises_key_error_when_secret_missing(self) -> None:
        """Verify KeyError when the secret does not exist in the vault."""
        # Arrange
        mock_client_instance = MagicMock()
        mock_client_instance.get_secret.side_effect = _FakeResourceNotFoundError("not found")
        mock_client_cls = MagicMock(return_value=mock_client_instance)
        mock_credential_cls = MagicMock()

        provider = AzureKeyVaultProvider(vault_url="https://my-vault.vault.azure.net")

        modules = _azure_modules(client_cls=mock_client_cls, credential_cls=mock_credential_cls)
        with (
            patch.dict("sys.modules", modules),
            pytest.raises(KeyError, match="Secret not found in Azure Key Vault"),
        ):
            provider.get("NONEXISTENT_KEY")

    def test_get_raises_key_error_when_value_is_none(self) -> None:
        """Verify KeyError when the secret exists but has a None value."""
        # Arrange
        mock_secret = MagicMock()
        mock_secret.value = None

        mock_client_instance = MagicMock()
        mock_client_instance.get_secret.return_value = mock_secret
        mock_client_cls = MagicMock(return_value=mock_client_instance)
        mock_credential_cls = MagicMock()

        provider = AzureKeyVaultProvider(vault_url="https://my-vault.vault.azure.net")

        modules = _azure_modules(client_cls=mock_client_cls, credential_cls=mock_credential_cls)
        with (
            patch.dict("sys.modules", modules),
            pytest.raises(KeyError, match="Secret has no value"),
        ):
            provider.get("EMPTY_SECRET")

    def test_import_error_when_azure_not_installed(self) -> None:
        """Verify helpful ImportError when azure deps are missing."""
        # Arrange
        provider = AzureKeyVaultProvider(vault_url="https://my-vault.vault.azure.net")

        # Setting a module value to None in sys.modules causes imports to raise
        # ImportError, which is the standard way to simulate missing packages.
        blocked_modules = {
            "azure": None,
            "azure.core": None,
            "azure.core.exceptions": None,
            "azure.identity": None,
            "azure.keyvault": None,
            "azure.keyvault.secrets": None,
        }
        with (
            patch.dict("sys.modules", blocked_modules),
            pytest.raises(ImportError, match="Azure Key Vault dependencies"),
        ):
            provider.get("SOME_SECRET")

    def test_client_is_cached_after_first_call(self) -> None:
        """Verify the SecretClient is created once and reused."""
        # Arrange
        mock_secret = MagicMock()
        mock_secret.value = "cached"

        mock_client_instance = MagicMock()
        mock_client_instance.get_secret.return_value = mock_secret
        mock_client_cls = MagicMock(return_value=mock_client_instance)
        mock_credential_cls = MagicMock()

        provider = AzureKeyVaultProvider(vault_url="https://my-vault.vault.azure.net")

        modules = _azure_modules(client_cls=mock_client_cls, credential_cls=mock_credential_cls)
        with patch.dict("sys.modules", modules):
            # Act — call get() twice
            provider.get("KEY_ONE")
            provider.get("KEY_TWO")

        # Assert — SecretClient constructor called exactly once
        mock_client_cls.assert_called_once()
        assert mock_client_instance.get_secret.call_count == 2
