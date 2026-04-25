import json

from msai.api._common import error_response


def test_error_response_envelope_shape() -> None:
    resp = error_response(422, "VALIDATION_ERROR", "bad input")
    assert resp.status_code == 422
    body = json.loads(resp.body)
    assert body == {"error": {"code": "VALIDATION_ERROR", "message": "bad input"}}
