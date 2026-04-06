from unittest.mock import Mock, patch

from msai.core.auth import EntraIDValidator


@patch("msai.core.auth.jwt.decode")
@patch("msai.core.auth.PyJWKClient")
def test_validate_token_decodes_with_expected_params(mock_jwk: Mock, mock_decode: Mock) -> None:
    mock_jwk.return_value.get_signing_key_from_jwt.return_value = Mock(key="pub")
    mock_decode.return_value = {"sub": "u1"}

    validator = EntraIDValidator("tenant", "client")
    claims = validator.validate_token("token")

    assert claims["sub"] == "u1"
    mock_decode.assert_called_once()
