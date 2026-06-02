import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
)

from msai.services.live.broker_credentials_store import KvFailureReason, classify_kv_exception


def _http(status):
    e = HttpResponseError(message="x")
    e.status_code = status
    return e


@pytest.mark.parametrize("exc,expected", [
    (ClientAuthenticationError("no token"), KvFailureReason.UNAUTHORIZED),
    (_http(401), KvFailureReason.UNAUTHORIZED),
    (_http(403), KvFailureReason.UNAUTHORIZED),
    (ResourceNotFoundError("missing"), KvFailureReason.NOT_FOUND),
    (_http(429), KvFailureReason.THROTTLED),
    (ServiceRequestError("dns"), KvFailureReason.UNREACHABLE),
    (_http(500), KvFailureReason.UNREACHABLE),  # unknown http → unreachable (fail-safe)
])
def test_classify_kv_exception(exc, expected):
    assert classify_kv_exception(exc) == expected


def test_envfile_store_put_get_rotate_delete(tmp_path):
    from msai.services.live.broker_credentials_store import (
        CredentialResolutionError,
        Credentials,
        EnvFileBrokerCredentialsStore,
    )

    store = EnvFileBrokerCredentialsStore(path=tmp_path / "creds.json")
    r1 = store.put("broker-cred-abc", Credentials("u1", "p1"), actor="op@x")
    assert r1.secret_ref == "broker-cred-abc" and r1.version is not None
    assert store.get(r1.secret_ref, r1.version) == Credentials("u1", "p1")
    r2 = store.rotate("broker-cred-abc", Credentials("u2", "p2"), actor="op@x")
    assert r2.version != r1.version
    assert store.get(r2.secret_ref, r2.version) == Credentials("u2", "p2")
    assert store.get(r1.secret_ref, r1.version) == Credentials("u1", "p1")  # old version retained
    store.delete(r1.secret_ref)
    with pytest.raises(CredentialResolutionError):
        store.get(r1.secret_ref, r2.version)
    assert store.ping() is True


def test_envfile_store_file_is_owner_only_after_put(tmp_path):
    # finding 5: the cleartext dev credentials file must be 0o600 (owner-only)
    # both at creation and after every atomic write.
    import os
    import stat

    import pytest

    from msai.services.live.broker_credentials_store import (
        Credentials,
        EnvFileBrokerCredentialsStore,
    )

    if os.name != "posix":
        pytest.skip("file-mode assertion is POSIX-only")

    path = tmp_path / "creds.json"
    store = EnvFileBrokerCredentialsStore(path=path)
    # owner-only at creation (before any write)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    store.put("broker-cred-abc", Credentials("u", "p"), actor="op@x")
    # still owner-only after the atomic write-through
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_envfile_store_write_oserror_fails_decrypt(tmp_path, monkeypatch):
    # finding 7: a dev disk failure on write must fail closed with a
    # classifiable CredentialResolutionError(DECRYPT_FAILED) — symmetry with
    # _read — so _best_effort_delete_secret (which catches that error) covers it.
    import os

    import pytest

    from msai.services.live.broker_credentials_store import (
        CredentialResolutionError,
        Credentials,
        EnvFileBrokerCredentialsStore,
        KvFailureReason,
    )

    store = EnvFileBrokerCredentialsStore(path=tmp_path / "creds.json")

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(os, "open", _boom)
    with pytest.raises(CredentialResolutionError) as ei:
        store.put("broker-cred-abc", Credentials("u", "p"), actor="op@x")
    assert ei.value.reason == KvFailureReason.DECRYPT_FAILED


def test_envfile_store_post_replace_chmod_failure_does_not_fail_write(tmp_path, monkeypatch):
    # finding 4 (iter-3 P3): the post-replace _chmod_owner_only is best-effort
    # and lives OUTSIDE the write-failure try. A chmod hiccup after a SUCCESSFUL
    # atomic replace must NOT report the write as DECRYPT_FAILED — the data is
    # already durably written and must be readable.
    import os

    from msai.services.live.broker_credentials_store import (
        Credentials,
        EnvFileBrokerCredentialsStore,
    )

    if os.name != "posix":
        pytest.skip("chmod assertion is POSIX-only")

    store = EnvFileBrokerCredentialsStore(path=tmp_path / "creds.json")

    real_chmod = os.chmod

    def _flaky_chmod(path, mode, *a, **kw):
        # blow up only on the post-write chmod of the store file, not the
        # 0o600-at-creation path (which already ran in __init__).
        raise OSError("chmod hiccup")

    monkeypatch.setattr(os, "chmod", _flaky_chmod)
    # write must SUCCEED despite the chmod failure
    res = store.put("broker-cred-abc", Credentials("u", "p"), actor="op@x")
    monkeypatch.setattr(os, "chmod", real_chmod)
    assert res.version is not None
    assert store.get("broker-cred-abc", res.version) == Credentials("u", "p")


def test_azure_kv_store_put_pins_returned_version_and_maps_errors(monkeypatch):
    from azure.core.exceptions import ResourceNotFoundError

    from msai.services.live import broker_credentials_store as mod  # noqa: F401
    from msai.services.live.broker_credentials_store import (
        AzureKvBrokerCredentialsStore,
        CredentialResolutionError,
        Credentials,
        KvFailureReason,
    )

    class FakeProps:
        version = "ver-xyz"

    class FakeSecret:
        def __init__(self):
            self.properties = FakeProps()
            self.value = "u\x1fp"

    class FakeClient:
        def set_secret(self, name, value, **kw):
            return FakeSecret()

        def get_secret(self, name, version=None, **kw):
            if version == "missing":
                raise ResourceNotFoundError("nope")
            return FakeSecret()

        def begin_delete_secret(self, name):
            class P:
                def wait(self):
                    pass

            return P()

    store = AzureKvBrokerCredentialsStore(client=FakeClient())
    res = store.put("broker-cred-1", Credentials("u", "p"), actor="op@x")
    assert res.version == "ver-xyz"  # pinned from properties.version, not parsed from .id
    assert store.get("broker-cred-1", "ver-xyz") == Credentials("u", "p")
    with pytest.raises(CredentialResolutionError) as ei:
        store.get("broker-cred-1", "missing")
    assert ei.value.reason == KvFailureReason.NOT_FOUND


def test_factory_selects_envfile_in_dev(monkeypatch, tmp_path):
    from msai.services.live.broker_credentials_store import (
        EnvFileBrokerCredentialsStore,
        get_broker_credentials_store,
    )

    store = get_broker_credentials_store(environment="development", data_root=tmp_path)
    assert isinstance(store, EnvFileBrokerCredentialsStore)


def test_envfile_store_corrupt_json_fails_decrypt(tmp_path):
    # finding 9: a corrupt/partial JSON file must fail closed with
    # DECRYPT_FAILED, not a bare JSONDecodeError callers can't classify.
    from msai.services.live.broker_credentials_store import (
        CredentialResolutionError,
        EnvFileBrokerCredentialsStore,
        KvFailureReason,
    )

    path = tmp_path / "creds.json"
    store = EnvFileBrokerCredentialsStore(path=path)
    path.write_text("{not valid json")  # corrupt the backing file
    with pytest.raises(CredentialResolutionError) as ei:
        store.get("broker-cred-abc", "someversion")
    assert ei.value.reason == KvFailureReason.DECRYPT_FAILED


def test_envfile_store_entry_missing_versions_fails_decrypt(tmp_path):
    # finding 9: an entry present but missing its "versions" map (partial write)
    # must fail closed with DECRYPT_FAILED, not a bare KeyError.
    import json

    from msai.services.live.broker_credentials_store import (
        CredentialResolutionError,
        EnvFileBrokerCredentialsStore,
        KvFailureReason,
    )

    path = tmp_path / "creds.json"
    store = EnvFileBrokerCredentialsStore(path=path)
    path.write_text(json.dumps({"broker-cred-abc": {"latest": "v1"}}))  # no "versions" key
    with pytest.raises(CredentialResolutionError) as ei:
        store.get("broker-cred-abc", "v1")
    assert ei.value.reason == KvFailureReason.DECRYPT_FAILED


@pytest.mark.parametrize("root", ["[]", '"a string"', "42", "null"])
def test_envfile_store_non_dict_root_fails_decrypt(tmp_path, root):
    # Codex iter-10 P3: valid JSON but wrong top-level type (e.g. `[]` after a manual
    # edit) must classify as DECRYPT_FAILED, not crash later with a bare
    # AttributeError/TypeError that bypasses the CredentialResolutionError handling.
    from msai.services.live.broker_credentials_store import (
        CredentialResolutionError,
        Credentials,
        EnvFileBrokerCredentialsStore,
        KvFailureReason,
    )

    path = tmp_path / "creds.json"
    store = EnvFileBrokerCredentialsStore(path=path)
    path.write_text(root)
    # get, put, and delete all route through _read() → must fail closed
    with pytest.raises(CredentialResolutionError) as ei:
        store.get("broker-cred-abc", "v1")
    assert ei.value.reason == KvFailureReason.DECRYPT_FAILED
    with pytest.raises(CredentialResolutionError):
        store.put("broker-cred-abc", Credentials("u", "p"), actor="op@x")
    with pytest.raises(CredentialResolutionError):
        store.delete("broker-cred-abc")


@pytest.mark.parametrize(
    "rec",
    [
        {"u": "user"},  # missing "p"
        {"p": "pass"},  # missing "u"
        "not-a-dict",  # non-dict record (manual corruption)
        12345,  # non-dict record
    ],
)
def test_envfile_store_malformed_version_record_fails_decrypt(tmp_path, rec):
    # Codex iter-8 P3: a version record that is not a dict, or is missing u/p,
    # must classify as DECRYPT_FAILED instead of escaping as a bare KeyError/TypeError
    # past resolve_for_spawn's CredentialResolutionError handler.
    import json

    from msai.services.live.broker_credentials_store import (
        CredentialResolutionError,
        EnvFileBrokerCredentialsStore,
        KvFailureReason,
    )

    path = tmp_path / "creds.json"
    store = EnvFileBrokerCredentialsStore(path=path)
    path.write_text(json.dumps({"broker-cred-abc": {"latest": "v1", "versions": {"v1": rec}}}))
    with pytest.raises(CredentialResolutionError) as ei:
        store.get("broker-cred-abc", "v1")
    assert ei.value.reason == KvFailureReason.DECRYPT_FAILED


def test_azure_kv_store_rejects_null_version_and_malformed_payload():
    from msai.services.live.broker_credentials_store import (
        AzureKvBrokerCredentialsStore,
        CredentialResolutionError,
        KvFailureReason,
    )

    class FakeProps:
        version = "v"

    class FakeMalformed:
        def __init__(self, value):
            self.properties = FakeProps()
            self.value = value

    class FakeClient:
        def __init__(self, value):
            self._value = value

        def get_secret(self, name, version=None, **kw):
            return FakeMalformed(self._value)

    # null version → DECRYPT_FAILED (never silently read "latest")
    store_ok = AzureKvBrokerCredentialsStore(client=FakeClient("u\x1fp"))
    with pytest.raises(CredentialResolutionError) as e1:
        store_ok.get("broker-cred-1", None)
    assert e1.value.reason == KvFailureReason.DECRYPT_FAILED
    # malformed payload (no separator) → DECRYPT_FAILED, NOT an empty password
    store_bad = AzureKvBrokerCredentialsStore(client=FakeClient("no-separator"))
    with pytest.raises(CredentialResolutionError) as e2:
        store_bad.get("broker-cred-1", "v")
    assert e2.value.reason == KvFailureReason.DECRYPT_FAILED
