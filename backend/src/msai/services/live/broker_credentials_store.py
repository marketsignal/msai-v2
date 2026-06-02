from __future__ import annotations

import contextlib
import json
import os
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, runtime_checkable

from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
)


class KvFailureReason(StrEnum):
    UNAUTHORIZED = "kv_unauthorized"
    NOT_FOUND = "kv_not_found"
    THROTTLED = "kv_throttled"
    UNREACHABLE = "kv_unreachable"
    # malformed/undecodable secret payload or missing pinned version
    # (council reason list — decision doc line 257)
    DECRYPT_FAILED = "decrypt_failed"


class CredentialResolutionError(RuntimeError):
    """Raised when reading/writing credentials fails. `reason` maps to SPAWN_FAILED_PERMANENT."""

    def __init__(self, reason: KvFailureReason, account_ref: str, message: str) -> None:
        self.reason = reason
        self.account_ref = account_ref
        super().__init__(f"[{reason}] {account_ref}: {message}")


@dataclass(frozen=True, slots=True)
class Credentials:
    tws_userid: str
    tws_password: str


@dataclass(frozen=True, slots=True)
class CredentialWriteResult:
    secret_ref: str
    version: str | None


def classify_kv_exception(exc: Exception) -> KvFailureReason:
    # Order matters: specific subclasses before broad HttpResponseError; ServiceRequestError
    # is NOT an HttpResponseError (research finding #3).
    if isinstance(exc, ResourceNotFoundError):
        return KvFailureReason.NOT_FOUND
    if isinstance(exc, ClientAuthenticationError):
        return KvFailureReason.UNAUTHORIZED
    if isinstance(exc, ServiceRequestError):
        return KvFailureReason.UNREACHABLE
    if isinstance(exc, HttpResponseError):
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return KvFailureReason.UNAUTHORIZED
        if status == 429:
            return KvFailureReason.THROTTLED
        return KvFailureReason.UNREACHABLE  # fail-safe for unknown HTTP errors
    return KvFailureReason.UNREACHABLE


@runtime_checkable
class BrokerCredentialsStore(Protocol):
    def put(self, account_ref: str, creds: Credentials, *, actor: str) -> CredentialWriteResult: ...
    def get(self, secret_ref: str, version: str | None) -> Credentials: ...
    def rotate(
        self, account_ref: str, creds: Credentials, *, actor: str
    ) -> CredentialWriteResult: ...
    def delete(self, secret_ref: str) -> None: ...
    def ping(self) -> bool: ...  # boot reachability probe


class EnvFileBrokerCredentialsStore:
    """Dev/test writable store. JSON file under DATA_ROOT (gitignored). Per-ref version
    list mirrors KV versioning so dev exercises the same get(ref, version) contract."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            # Create owner-only so the cleartext dev store is never group/world
            # readable from the moment it exists.
            fd = os.open(str(self._path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write("{}")
        self._chmod_owner_only(self._path)

    def _read(self) -> dict[str, Any]:
        try:
            data: Any = json.loads(self._path.read_text() or "{}")
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt/partial file must fail closed, not crash with a bare
            # JSONDecodeError that callers can't classify.
            raise CredentialResolutionError(
                KvFailureReason.DECRYPT_FAILED, str(self._path), f"unreadable store: {exc}"
            ) from exc
        # Valid JSON but wrong top-level type (e.g. ``[]`` after a manual edit) would
        # otherwise raise a bare AttributeError/TypeError downstream, bypassing the
        # CredentialResolutionError classification + best-effort cleanup (Codex iter-10 P3).
        if not isinstance(data, dict):
            raise CredentialResolutionError(
                KvFailureReason.DECRYPT_FAILED,
                str(self._path),
                f"store root must be a JSON object, got {type(data).__name__}",
            )
        return data

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            # 0o600 so the cleartext dev store is owner-only. os.open with the
            # mode set at creation avoids a brief world-readable window for the
            # temp file before the atomic replace.
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(data))
            tmp.replace(self._path)  # atomic
        except OSError as exc:
            # Symmetry with _read: a dev disk failure must fail closed with a
            # classifiable error so _best_effort_delete_secret (which catches
            # CredentialResolutionError) covers it too. Only failures of the
            # actual write/replace land here — the temp file is already created
            # 0o600 above, so the owner-only guarantee holds before the replace.
            raise CredentialResolutionError(
                KvFailureReason.DECRYPT_FAILED, str(self._path), f"unwritable store: {exc}"
            ) from exc
        # finding 4 (iter-3 P3): the post-replace chmod is a SEPARATE best-effort
        # step OUTSIDE the write-failure try. The data is already durably written;
        # a chmod hiccup here must not masquerade a SUCCESSFUL write as a write
        # failure. The temp file was created 0o600 so the file is owner-only the
        # whole time regardless.
        self._chmod_owner_only(self._path)

    @staticmethod
    def _chmod_owner_only(path: Path) -> None:
        """Best-effort 0o600 on ``path`` (no-op on non-POSIX / chmod failure).

        Owner-only is already guaranteed by the 0o600 temp-file creation in
        ``_write`` before the atomic replace; this re-assertion is belt-and-braces
        and must never fail a successful write, so a chmod ``OSError`` is
        swallowed (logged at debug — nothing actionable for a dev store).
        """
        if os.name != "posix":
            return
        # Best-effort: the file is already 0o600 from temp-file creation, so a
        # re-assert failure is non-actionable and must not surface as a write
        # failure (finding 4 / iter-3 P3).
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    def put(self, account_ref: str, creds: Credentials, *, actor: str) -> CredentialWriteResult:
        return self.rotate(account_ref, creds, actor=actor)

    def rotate(self, account_ref: str, creds: Credentials, *, actor: str) -> CredentialWriteResult:
        version = uuid.uuid4().hex
        with self._lock:
            data = self._read()
            entry = data.setdefault(account_ref, {"versions": {}})
            entry["versions"][version] = {"u": creds.tws_userid, "p": creds.tws_password}
            entry["latest"] = version
            self._write(data)
        return CredentialWriteResult(secret_ref=account_ref, version=version)

    def get(self, secret_ref: str, version: str | None) -> Credentials:
        if not version:  # mirror Azure pinned-version semantics (Codex iter-2 P2) — never "latest"
            raise CredentialResolutionError(
                KvFailureReason.DECRYPT_FAILED, secret_ref, "missing pinned secret version"
            )
        data = self._read()
        entry = data.get(secret_ref)
        if entry is None:
            raise CredentialResolutionError(KvFailureReason.NOT_FOUND, secret_ref, "no such secret")
        versions = entry.get("versions") if isinstance(entry, dict) else None
        if not isinstance(versions, dict):
            # Partial/corrupt entry missing its "versions" map — fail closed.
            raise CredentialResolutionError(
                KvFailureReason.DECRYPT_FAILED, secret_ref, "entry missing versions map"
            )
        rec = versions.get(version)
        if rec is None:
            raise CredentialResolutionError(
                KvFailureReason.NOT_FOUND, secret_ref, f"version {version} missing"
            )
        # Classify a malformed/corrupt record (non-dict, or missing u/p) as DECRYPT_FAILED
        # rather than letting a bare KeyError/TypeError escape unclassified past
        # resolve_for_spawn's CredentialResolutionError handler (Codex iter-8 P3).
        if not isinstance(rec, dict) or "u" not in rec or "p" not in rec:
            raise CredentialResolutionError(
                KvFailureReason.DECRYPT_FAILED, secret_ref, f"version {version} record is malformed"
            )
        return Credentials(rec["u"], rec["p"])

    def delete(self, secret_ref: str) -> None:
        with self._lock:
            data = self._read()
            data.pop(secret_ref, None)
            self._write(data)

    def ping(self) -> bool:
        return self._path.parent.exists()


_SEP = "\x1f"  # unit separator — not valid in IB usernames/passwords


class AzureKvBrokerCredentialsStore:
    """Prod store backed by Azure Key Vault. `client` is an azure.keyvault.secrets.SecretClient
    (injected so tests use a fake — never hit live KV). userid/password are packed into a single
    secret value separated by `_SEP`; the returned version is pinned from `properties.version`."""

    def __init__(self, client: Any) -> None:  # client: azure.keyvault.secrets.SecretClient
        self._client = client

    def put(self, account_ref: str, creds: Credentials, *, actor: str) -> CredentialWriteResult:
        return self.rotate(account_ref, creds, actor=actor)

    def rotate(
        self, account_ref: str, creds: Credentials, *, actor: str
    ) -> CredentialWriteResult:
        value = f"{creds.tws_userid}{_SEP}{creds.tws_password}"
        try:
            result = self._client.set_secret(account_ref, value, tags={"updated_by": actor})
        except Exception as exc:  # noqa: BLE001 — classified + re-raised
            raise CredentialResolutionError(
                classify_kv_exception(exc), account_ref, str(exc)
            ) from exc
        return CredentialWriteResult(secret_ref=account_ref, version=result.properties.version)

    def get(self, secret_ref: str, version: str | None) -> Credentials:
        if not version:  # pinned-version semantics: never silently read "latest"
            raise CredentialResolutionError(
                KvFailureReason.DECRYPT_FAILED, secret_ref, "missing pinned secret version"
            )
        try:
            secret = self._client.get_secret(secret_ref, version)
        except Exception as exc:  # noqa: BLE001
            raise CredentialResolutionError(
                classify_kv_exception(exc), secret_ref, str(exc)
            ) from exc
        raw = secret.value or ""
        if _SEP not in raw:  # malformed payload — do NOT return an empty password
            raise CredentialResolutionError(
                KvFailureReason.DECRYPT_FAILED, secret_ref, "secret payload missing separator"
            )
        userid, _, password = raw.partition(_SEP)
        if not userid or not password:
            raise CredentialResolutionError(
                KvFailureReason.DECRYPT_FAILED,
                secret_ref,
                "secret payload has empty userid/password",
            )
        return Credentials(userid, password)

    def delete(self, secret_ref: str) -> None:
        try:
            self._client.begin_delete_secret(secret_ref).wait()
        except Exception as exc:  # noqa: BLE001
            raise CredentialResolutionError(
                classify_kv_exception(exc), secret_ref, str(exc)
            ) from exc

    def ping(self) -> bool:
        # cheap reachability probe; list one secret property page
        try:
            iterator = self._client.list_properties_of_secrets()
            next(iter(iterator), None)
            return True
        except Exception:  # noqa: BLE001
            return False


def get_broker_credentials_store(
    *,
    environment: str,
    data_root: str | Path,
    kv_uri: str | None = None,
    mi_client_id: str | None = None,
) -> BrokerCredentialsStore:
    """Construct the environment-appropriate :class:`BrokerCredentialsStore`.

    Production binds to Azure Key Vault via the VM's managed identity; dev/test
    use a file-backed store under ``data_root``. Azure SDK clients are imported
    lazily so the dev path never pulls in ``azure.identity`` / ``azure.keyvault``.
    """
    if environment == "production":
        # Lazy import so dev/test never resolves the Azure SDK at module load.
        from azure.identity import ManagedIdentityCredential
        from azure.keyvault.secrets import SecretClient

        if not kv_uri:
            raise RuntimeError(
                "AZURE_KEYVAULT_URI required in production for broker credentials"
            )
        # Codex iter-3 P1: use an EXPLICIT ManagedIdentityCredential — NOT
        # DefaultAzureCredential. The container exports AZURE_CLIENT_ID as the
        # Entra JWT audience; DefaultAzureCredential would consume it and misroute
        # to the WRONG identity. ManagedIdentityCredential() = the VM's
        # system-assigned MI; an explicit user-assigned id arrives ONLY via the
        # dedicated AZURE_KV_MI_CLIENT_ID env (NEVER the JWT AZURE_CLIENT_ID).
        credential = (
            ManagedIdentityCredential(client_id=mi_client_id)
            if mi_client_id
            else ManagedIdentityCredential()
        )
        client = SecretClient(vault_url=kv_uri, credential=credential)
        return AzureKvBrokerCredentialsStore(client=client)
    # gitignored subdir (Codex iter-1 P0#1) — see Task 4 .gitignore step
    return EnvFileBrokerCredentialsStore(
        path=Path(data_root) / "broker_credentials" / "dev_broker_credentials.json"
    )
