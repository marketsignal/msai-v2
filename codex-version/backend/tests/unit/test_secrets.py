import os

import pytest

from msai.core.secrets import EnvSecretsProvider


def test_env_secrets_provider_returns_value() -> None:
    os.environ["X_SECRET"] = "abc"
    assert EnvSecretsProvider().get("X_SECRET") == "abc"


def test_env_secrets_provider_missing_raises() -> None:
    os.environ.pop("MISSING_SECRET", None)
    with pytest.raises(KeyError):
        EnvSecretsProvider().get("MISSING_SECRET")
