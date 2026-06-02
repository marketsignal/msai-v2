"""Integration tests for the ``/api/v1/broker-accounts`` API router.

These exercise the full HTTP contract against a Postgres testcontainer
(via ``api_client_authed``) and the file-backed dev credentials store.
The central invariant: TWS credentials submitted on create/rotate are
written server-side to the secrets backend and are NEVER echoed back in
any response body — only credential references + audit columns appear.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from msai.main import app
from msai.services.live.broker_credentials_store import EnvFileBrokerCredentialsStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest_asyncio.fixture
def broker_store(tmp_path: Path) -> Iterator[EnvFileBrokerCredentialsStore]:
    """Wire a file-backed dev credentials store onto the shared app state.

    The httpx ASGITransport does not trigger the app lifespan, so
    ``app.state.broker_credentials_store`` is never populated by the
    production startup path during tests. Set it explicitly here.
    """
    store = EnvFileBrokerCredentialsStore(tmp_path / "broker_creds.json")
    previous = getattr(app.state, "broker_credentials_store", None)
    app.state.broker_credentials_store = store
    yield store
    app.state.broker_credentials_store = previous


@pytest.mark.asyncio
async def test_create_list_get_no_secret_leak(api_client_authed, broker_store) -> None:
    body = {
        "ib_account_id": "DU111",
        "ib_login_key": "L1",
        "trading_mode": "paper",
        "tws_userid": "u",
        "tws_password": "p",
    }
    r = await api_client_authed.post("/api/v1/broker-accounts", json=body)
    assert r.status_code == 201, r.text
    assert "Location" in r.headers
    created = r.json()
    assert created["ib_account_id"] == "DU111"
    assert "tws_userid" not in created and "tws_password" not in created
    assert created["credentials_secret_ref"] and created["credentials_secret_version"]
    acct_id = created["id"]

    # follow Location
    got = await api_client_authed.get(r.headers["Location"])
    assert got.status_code == 200 and got.json()["id"] == acct_id
    assert "tws_password" not in got.text

    lst = await api_client_authed.get("/api/v1/broker-accounts")
    assert lst.status_code == 200
    assert any(a["id"] == acct_id for a in lst.json())  # bare list, matches portfolios.py:97


@pytest.mark.asyncio
async def test_create_persists_created_by(api_client_authed, broker_store) -> None:
    # finding 1: the router resolves the JWT subject to a users.id and passes it
    # through to the service, which persists it to created_by (previously the
    # resolved id was discarded → created_by always NULL).
    body = {
        "ib_account_id": "DU777",
        "ib_login_key": "L7",
        "trading_mode": "paper",
        "tws_userid": "u",
        "tws_password": "p",
    }
    created = (await api_client_authed.post("/api/v1/broker-accounts", json=body)).json()
    assert created["created_by"] is not None
    # the audit column survives a re-read
    got = await api_client_authed.get(f"/api/v1/broker-accounts/{created['id']}")
    assert got.json()["created_by"] == created["created_by"]


@pytest.mark.asyncio
async def test_blank_ib_login_key_422(api_client_authed, broker_store) -> None:
    # finding 2: a blank ib_login_key is rejected at the schema boundary (422).
    body = {
        "ib_account_id": "DU778",
        "ib_login_key": "",
        "trading_mode": "paper",
        "tws_userid": "u",
        "tws_password": "p",
    }
    r = await api_client_authed.post("/api/v1/broker-accounts", json=body)
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_live_account_with_paper_mode_422(api_client_authed, broker_store) -> None:
    # finding 1 (iter-3 P2): a U... (live) account with trading_mode=paper is
    # rejected at the schema boundary (422) — mirrors the live-start/CLI guard,
    # closing nautilus gotcha #6 at the broker-account boundary.
    body = {
        "ib_account_id": "U4705114",
        "ib_login_key": "lvp",
        "trading_mode": "paper",
        "tws_userid": "u",
        "tws_password": "p",
    }
    r = await api_client_authed.post("/api/v1/broker-accounts", json=body)
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_paper_account_with_live_mode_422(api_client_authed, broker_store) -> None:
    # finding 1 (iter-3 P2): a DU... (paper) account with trading_mode=live is
    # rejected at the schema boundary (422).
    body = {
        "ib_account_id": "DU1234567",
        "ib_login_key": "lvp",
        "trading_mode": "live",
        "tws_userid": "u",
        "tws_password": "p",
    }
    r = await api_client_authed.post("/api/v1/broker-accounts", json=body)
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_rotate_advances_version(api_client_authed, broker_store) -> None:
    body = {
        "ib_account_id": "DU222",
        "ib_login_key": "L2",
        "trading_mode": "paper",
        "tws_userid": "u",
        "tws_password": "p",
    }
    created = (await api_client_authed.post("/api/v1/broker-accounts", json=body)).json()
    v1 = created["credentials_secret_version"]
    rot = await api_client_authed.post(
        f"/api/v1/broker-accounts/{created['id']}/rotate-credentials",
        json={"tws_userid": "u2", "tws_password": "p2"},
    )
    assert rot.status_code == 200, rot.text
    assert "tws_password" not in rot.text
    v2 = (await api_client_authed.get(f"/api/v1/broker-accounts/{created['id']}")).json()[
        "credentials_secret_version"
    ]
    assert v2 and v2 != v1


@pytest.mark.asyncio
async def test_rotate_on_archived_account_409(api_client_authed, broker_store) -> None:
    # finding 3: rotating an archived account is rejected — AccountArchivedError
    # maps to 409 (checked BEFORE the base BrokerAccountError → 422).
    body = {
        "ib_account_id": "DU779",
        "ib_login_key": "L7b",
        "trading_mode": "paper",
        "tws_userid": "u",
        "tws_password": "p",
    }
    created = (await api_client_authed.post("/api/v1/broker-accounts", json=body)).json()
    arch = await api_client_authed.post(f"/api/v1/broker-accounts/{created['id']}/archive")
    assert arch.status_code == 200, arch.text
    rot = await api_client_authed.post(
        f"/api/v1/broker-accounts/{created['id']}/rotate-credentials",
        json={"tws_userid": "u2", "tws_password": "p2"},
    )
    assert rot.status_code == 409, rot.text


@pytest.mark.asyncio
async def test_duplicate_active_account_conflicts(api_client_authed, broker_store) -> None:
    body = {
        "ib_account_id": "DU333",
        "ib_login_key": "L3",
        "trading_mode": "paper",
        "tws_userid": "u",
        "tws_password": "p",
    }
    assert (await api_client_authed.post("/api/v1/broker-accounts", json=body)).status_code == 201
    assert (await api_client_authed.post("/api/v1/broker-accounts", json=body)).status_code == 409


@pytest.mark.asyncio
async def test_archived_account_listable_only_with_include_archived(
    api_client_authed, broker_store
) -> None:
    # Codex final-review P2: an archived account must stay discoverable when
    # explicitly requested via ?include_archived=true, but be excluded by default.
    body = {
        "ib_account_id": "DU444",
        "ib_login_key": "L4",
        "trading_mode": "paper",
        "tws_userid": "u",
        "tws_password": "p",
    }
    created = (await api_client_authed.post("/api/v1/broker-accounts", json=body)).json()
    acct_id = created["id"]
    assert (
        await api_client_authed.post(f"/api/v1/broker-accounts/{acct_id}/archive")
    ).status_code == 200

    default_list = (await api_client_authed.get("/api/v1/broker-accounts")).json()
    assert not any(a["id"] == acct_id for a in default_list), "archived must be excluded by default"

    with_archived = (
        await api_client_authed.get("/api/v1/broker-accounts?include_archived=true")
    ).json()
    archived_row = next((a for a in with_archived if a["id"] == acct_id), None)
    assert archived_row is not None, "archived row must be discoverable when explicitly requested"
    assert archived_row["status"] == "archived"


@pytest.mark.asyncio
async def test_too_long_password_422_does_not_echo_cleartext(
    api_client_authed, broker_store
) -> None:
    # finding 4: a too-long password is rejected 422, and the cleartext must
    # NOT appear in the response body (SecretStr masks the 422 ``input`` echo).
    secret = "x" * 600  # over the 512 max_length
    body = {
        "ib_account_id": "DU444",
        "ib_login_key": "L4",
        "trading_mode": "paper",
        "tws_userid": "u",
        "tws_password": secret,
    }
    r = await api_client_authed.post("/api/v1/broker-accounts", json=body)
    assert r.status_code == 422, r.text
    assert secret not in r.text  # cleartext password never echoed back


@pytest.mark.asyncio
async def test_model_level_validation_422_does_not_echo_cleartext(
    api_client_authed, broker_store
) -> None:
    # P1 (iter-4): a cross-field MODEL-level validation error (here the
    # prefix-vs-mode model_validator: a U... live account paired with
    # trading_mode=paper) reports loc=("body",) with ``input`` = the ENTIRE
    # request body dict — which includes the cleartext tws_password. The
    # validation handler must redact sensitive keys inside that dict so the
    # cleartext NEVER appears anywhere in the 422 body. SecretStr masking only
    # covers field-level scalar input; this is the dict-level path.
    # A VALID-length password so the cross-field model_validator actually fires
    # (a too-long password would fail field validation first, short-circuiting
    # the model_validator and producing only a masked field-level error). This
    # exercises the loc=("body",) dict-input path specifically.
    secret = "supersecret-but-valid-length"  # within the 512 max_length
    body = {
        "ib_account_id": "U4705114",  # live-prefix account
        "ib_login_key": "lvp",
        "trading_mode": "paper",  # contradicts the U... prefix → model_validator
        "tws_userid": "secretuser",
        "tws_password": secret,
    }
    r = await api_client_authed.post("/api/v1/broker-accounts", json=body)
    assert r.status_code == 422, r.text
    # cleartext password must be absent ANYWHERE in the response body, even
    # though it was carried inside the body-dict ``input`` of a loc=("body",)
    # error item.
    assert secret not in r.text
    assert "secretuser" not in r.text


@pytest.mark.asyncio
async def test_get_missing_account_404(api_client_authed, broker_store) -> None:
    # finding 7: GET an unknown id → 404 with an explanatory body.
    import uuid

    r = await api_client_authed.get(f"/api/v1/broker-accounts/{uuid.uuid4()}")
    assert r.status_code == 404, r.text
    assert r.json().get("detail")


@pytest.mark.asyncio
async def test_unknown_pinned_gateway_slot_422(api_client_authed, broker_store) -> None:
    # finding 7: pinning a gateway_slot not in the configured pool → 422.
    body = {
        "ib_account_id": "DU555",
        "ib_login_key": "L5",
        "trading_mode": "paper",
        "gateway_slot": "not-a-real-slot",
        "tws_userid": "u",
        "tws_password": "p",
    }
    r = await api_client_authed.post("/api/v1/broker-accounts", json=body)
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_patch_does_not_change_immutable_ib_account_id(
    api_client_authed, broker_store
) -> None:
    # finding 7: ib_account_id is immutable — it is intentionally absent from the
    # PATCH schema, so a client that sends it has the field IGNORED (Pydantic
    # drops unmodelled keys) and the account keeps its original id. A mutable
    # field in the same PATCH still applies. (The service-level
    # ImmutableFieldError → 422 path is covered separately at the service level,
    # where the occupied-slot IntegrityError path also lives — both depend on
    # the migration-only partial indexes that ``api_client_authed``'s
    # ``create_all`` schema does not build.)
    body = {
        "ib_account_id": "DU558",
        "ib_login_key": "L5c",
        "trading_mode": "paper",
        "tws_userid": "u",
        "tws_password": "p",
    }
    created = (await api_client_authed.post("/api/v1/broker-accounts", json=body)).json()
    r = await api_client_authed.patch(
        f"/api/v1/broker-accounts/{created['id']}",
        json={"ib_account_id": "DU999", "label": "renamed"},
    )
    assert r.status_code == 200, r.text
    patched = r.json()
    assert patched["ib_account_id"] == "DU558"  # unchanged — immutable
    assert patched["label"] == "renamed"  # mutable field applied


@pytest.mark.asyncio
async def test_patch_explicit_null_label_clears_it(api_client_authed, broker_store) -> None:
    # finding 2 (iter-3 P2): PATCH {"label": null} explicitly clears the label;
    # a subsequent PATCH that omits label leaves it untouched. The router maps
    # 'key present' (model_fields_set) to a forwarded value and 'key absent' to
    # the service UNSET sentinel.
    body = {
        "ib_account_id": "DU560",
        "ib_login_key": "L5d",
        "trading_mode": "paper",
        "label": "initial",
        "tws_userid": "u",
        "tws_password": "p",
    }
    created = (await api_client_authed.post("/api/v1/broker-accounts", json=body)).json()
    assert created["label"] == "initial"
    acct_id = created["id"]

    # explicit null clears the label
    r = await api_client_authed.patch(f"/api/v1/broker-accounts/{acct_id}", json={"label": None})
    assert r.status_code == 200, r.text
    assert r.json()["label"] is None

    # re-fetch confirms the cleared label persisted
    got = await api_client_authed.get(f"/api/v1/broker-accounts/{acct_id}")
    assert got.json()["label"] is None

    # omitting label on a later PATCH leaves it unchanged (still null). The
    # account is a DU... (paper-prefix) one, so the consistent trading_mode is
    # "paper" — the prefix-vs-mode guard (iter-4 P2) now rejects a live switch.
    r2 = await api_client_authed.patch(
        f"/api/v1/broker-accounts/{acct_id}", json={"trading_mode": "paper"}
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["label"] is None
    assert r2.json()["trading_mode"] == "paper"


@pytest.mark.asyncio
async def test_patch_trading_mode_mismatch_422(api_client_authed, broker_store) -> None:
    # iter-4 P2: PATCHing a DU... (paper-prefix) account to trading_mode=live is
    # rejected by the shared prefix-vs-mode guard in update() and maps to 422 via
    # the router catch-all (BrokerAccountError → 422). Defends nautilus gotcha #6
    # against the update path, not just create.
    body = {
        "ib_account_id": "DU562",
        "ib_login_key": "L5e",
        "trading_mode": "paper",
        "tws_userid": "u",
        "tws_password": "p",
    }
    created = (await api_client_authed.post("/api/v1/broker-accounts", json=body)).json()
    acct_id = created["id"]
    r = await api_client_authed.patch(
        f"/api/v1/broker-accounts/{acct_id}", json={"trading_mode": "live"}
    )
    assert r.status_code == 422, r.text
    # the row is untouched: a re-read still shows paper
    got = await api_client_authed.get(f"/api/v1/broker-accounts/{acct_id}")
    assert got.json()["trading_mode"] == "paper"


@pytest.mark.asyncio
async def test_patch_explicit_null_trading_mode_422(api_client_authed, broker_store) -> None:
    # iter-5 P3-b: PATCH {"trading_mode": null} is an explicit null on a
    # non-nullable column — meaningless. The schema parses null as None (the key
    # IS in model_fields_set, so the router forwards it), and the service rejects
    # it (BrokerAccountError → 422 via the router catch-all). API-level contract
    # test for the existing service-level rejection.
    body = {
        "ib_account_id": "DU564",
        "ib_login_key": "L5f",
        "trading_mode": "paper",
        "tws_userid": "u",
        "tws_password": "p",
    }
    created = (await api_client_authed.post("/api/v1/broker-accounts", json=body)).json()
    acct_id = created["id"]
    r = await api_client_authed.patch(
        f"/api/v1/broker-accounts/{acct_id}", json={"trading_mode": None}
    )
    assert r.status_code == 422, r.text
    # the row is untouched: a re-read still shows the original paper mode
    got = await api_client_authed.get(f"/api/v1/broker-accounts/{acct_id}")
    assert got.json()["trading_mode"] == "paper"


@pytest.mark.asyncio
async def test_store_failure_maps_to_502(api_client_authed, monkeypatch) -> None:
    # finding 7: a credentials-store failure on create maps to 502 BAD_GATEWAY.
    from msai.main import app
    from msai.services.live.broker_credentials_store import (
        CredentialResolutionError,
        KvFailureReason,
    )

    class _BoomStore:
        def put(self, *a, **kw):
            raise CredentialResolutionError(KvFailureReason.UNREACHABLE, "broker-cred-x", "kv down")

        def get(self, *a, **kw):  # pragma: no cover - not reached
            raise CredentialResolutionError(KvFailureReason.UNREACHABLE, "broker-cred-x", "kv down")

        def rotate(self, *a, **kw):  # pragma: no cover - not reached
            raise CredentialResolutionError(KvFailureReason.UNREACHABLE, "broker-cred-x", "kv down")

        def delete(self, *a, **kw) -> None:  # pragma: no cover - not reached
            pass

        def ping(self) -> bool:  # pragma: no cover - not reached
            return False

    previous = getattr(app.state, "broker_credentials_store", None)
    app.state.broker_credentials_store = _BoomStore()
    try:
        body = {
            "ib_account_id": "DU560",
            "ib_login_key": "L6",
            "trading_mode": "paper",
            "tws_userid": "u",
            "tws_password": "p",
        }
        r = await api_client_authed.post("/api/v1/broker-accounts", json=body)
        assert r.status_code == 502, r.text
    finally:
        app.state.broker_credentials_store = previous
