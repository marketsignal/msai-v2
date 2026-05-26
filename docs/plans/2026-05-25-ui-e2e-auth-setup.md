# Auth Regression Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land three deterministic offline PR-time gates that block the May 20 MSAL → PyJWT regression class — Gate 1 (MSAL scope AST lint), Gate 2 (synthetic-JWT backend middleware contract test), Gate 3 (identity-contract.json cross-file consistency lint) — without any live Entra dependency.

**Architecture:** Custom ESLint rule (`@typescript-eslint/utils`) for Gate 1; pytest integration tests with patched JWKS + synthetic JWT hitting the real FastAPI dependency via `ASGITransport` for Gate 2; pytest file-walker validating `identity-contract.json` for Gate 3. Backend `validate_token` is extended (~10 LoC additive) to enforce `scp=access_as_user` and `ver=2.0`, closing the regression coverage gap the research brief surfaced. See `docs/superpowers/specs/2026-05-25-ui-e2e-auth-setup-design.md` for the full design + decision rationale.

**Tech Stack:** TypeScript / ESLint 9 flat config / `@typescript-eslint/utils` / `@typescript-eslint/rule-tester` (Gate 1). Python 3.12 / PyJWT[crypto]>=2.9.0 / cryptography>=43 / pytest / httpx + ASGITransport (Gates 2, 3 + extended `validate_token`). GitHub Actions with `pnpm` + `uv` caching.

---

## Approach Comparison (final — persisted per workflow Phase 3.2)

### Chosen Default

Three deterministic offline gates per design spec:

- **Gate 1:** Custom ESLint rule (`msai/msal-scopes-must-be-api-or-oidc`) via `@typescript-eslint/utils`, registered in ESLint 9 flat config; tested with `RuleTester`.
- **Gate 2:** New `backend/tests/integration/auth_gate/test_auth_contract.py` with synthetic JWT + patched JWKS hitting the real FastAPI dependency via `ASGITransport`; **extends `validate_token` in `backend/src/msai/core/auth.py` to enforce `scp=access_as_user` and `ver=2.0`** (~10 LoC additive).
- **Gate 3:** New `backend/tests/integration/auth_gate/test_identity_contract.py` (Python file-walker) that schema-validates `identity-contract.json` and asserts no `AZURE_TENANT_ID`/`AZURE_CLIENT_ID` drift across `.env.example` files, compose files, and workflows.
- **`identity-contract.json` at repo root** (non-secret, lint/test-only consumption).
- **CI:** 2 GH Actions jobs (`frontend-lint` + `backend-auth-gate`), both required for branch protection.
- **Negative-control commit pair** on this PR proves Gate 1 fires on real `User.Read` reintroduction.

### Best Credible Alternative

Same gate structure BUT do NOT extend `validate_token`. Re-scope Gate 2 to drop cases 3 (missing `scp`), 4 (`scp=User.Read`), and 8 (app-only `roles` no `scp`). Backend code unchanged; Gate 2 covers cases 1, 2, 5, 6, 7, 9, 10 only.

### Scoring (fixed axes)

| Axis                  | Default (extend `validate_token`) | Alternative (don't extend) |
| --------------------- | --------------------------------- | -------------------------- |
| Complexity            | L                                 | L                          |
| Blast Radius          | L                                 | L                          |
| Reversibility         | M                                 | H                          |
| Time to Validate      | L                                 | L                          |
| User/Correctness Risk | L                                 | M                          |

### Cheapest Falsifying Test

< 5 min — mint a token without `scp` claim, pass through `validate_token`. Default raises `MissingRequiredClaimError`; Alternative returns payload silently.

## Contrarian Verdict

**VALIDATE** (Codex Contrarian, Phase 3.1c, 2026-05-25). Quote:

> "The only serious objection is blast radius: `validate_token` is central to REST bearer auth and WebSocket JWT auth, so this is not a harmless test-only change. But given prod tokens are verified delegated v2 access tokens with `scp=access_as_user`, API keys bypass the path, and the Alternative knowingly preserves acceptance of ID/app-only/wrong-scope tokens with the correct `aud`/`iss`, Default is the stronger regression gate."

**Carry-over:** Task 11 explicitly verifies the WebSocket JWT path (`validate_token_or_api_key` at `auth.py:78-89`) is unaffected by the `validate_token` extension — i.e., live Entra-issued tokens that traverse the WS path also carry `scp`.

---

## File Structure

| File                                                            | Action          | Size      | Responsibility                                                                                                         |
| --------------------------------------------------------------- | --------------- | --------- | ---------------------------------------------------------------------------------------------------------------------- |
| `identity-contract.json`                                        | NEW (root)      | ~15 lines | Non-secret single source of truth (tenant_id, client_id, app_id_uri, issuer, scope, version)                           |
| `identity-contract.schema.json`                                 | NEW (root)      | ~40 lines | JSON Schema validating the above shape                                                                                 |
| `identity-contract.README.md`                                   | NEW (root)      | ~50 lines | Convention explanation: non-secret, lint-time only, future multi-tenant note                                           |
| `backend/src/msai/core/auth.py`                                 | EDIT (+~15 LoC) | ~220 LoC  | Extend `validate_token`: require `scp`, enforce `scp=access_as_user`, enforce `ver=2.0`                                |
| `backend/tests/unit/test_auth.py`                               | EDIT            | varies    | Update `_make_token` helper to include `scp` + `ver` defaults; existing tests still green                              |
| `backend/tests/integration/auth_gate/__init__.py`               | NEW             | empty     | Mark integration dir as a package                                                                                      |
| `backend/tests/integration/auth_gate/conftest.py`               | NEW             | ~80 LoC   | Network-block autouse, function-scoped override-pop after parent `_override_auth`, JWKS keypair fixture, token factory |
| `backend/tests/integration/auth_gate/test_auth_contract.py`     | NEW             | ~250 LoC  | Gate 2 — 1 positive + 10 negative cases parametrized; smoke test as first test                                         |
| `backend/tests/integration/auth_gate/test_identity_contract.py` | NEW             | ~150 LoC  | Gate 3 — schema validate + file-walker drift detection                                                                 |
| `frontend/eslint-rules/index.mjs`                               | NEW             | ~10 lines | Plugin entry — exports the rules under `msai` namespace                                                                |
| `frontend/eslint-rules/msal-scopes.mjs`                         | NEW             | ~150 LoC  | Gate 1 — custom ESLint rule                                                                                            |
| `frontend/eslint-rules/__tests__/msal-scopes.test.mjs`          | NEW             | ~120 LoC  | RuleTester fixtures (valid + invalid)                                                                                  |
| `frontend/eslint.config.mjs`                                    | EDIT            | varies    | Register `msai` plugin + enable `msai/msal-scopes-must-be-api-or-oidc` rule                                            |
| `frontend/package.json`                                         | EDIT            | varies    | Add `@typescript-eslint/utils` + `@typescript-eslint/rule-tester` devDeps                                              |
| `.github/workflows/auth-gate.yml`                               | NEW             | ~60 lines | 2 jobs: `frontend-lint` (pnpm) + `backend-auth-gate` (uv pytest)                                                       |

**Files explicitly NOT changed:**

- `frontend/src/lib/msal-config.ts` — already correct shape; rule lints it without changing it
- `backend/src/msai/core/config.py` — no settings changes
- `docker-compose.dev.yml` / `docker-compose.prod.yml` — they use env-var passthrough; no value drift to fix
- `backend/tests/conftest.py` — autouse override stays as-is; integration tests opt out locally

---

## Task 1 — Foundation: `identity-contract.json`, schema, README

**Goal:** Establish the single source of truth that Gates 1 and 3 consume.

**Files:**

- Create: `identity-contract.json`
- Create: `identity-contract.schema.json`
- Create: `identity-contract.README.md`

- [ ] **Step 1.1 — Write `identity-contract.schema.json`**

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$id": "https://msai.local/identity-contract.schema.json",
  "title": "MSAI Identity Contract",
  "description": "Non-secret single source of truth for Entra ID identity wiring across MSAI v2. Consumed by Gate 1 (MSAL scope lint) and Gate 3 (cross-file consistency lint). NEVER contains secrets — only public OAuth identifiers.",
  "type": "object",
  "required": [
    "tenant_id",
    "client_id",
    "app_id_uri",
    "issuer",
    "scope_name",
    "token_version",
    "env_var_names"
  ],
  "additionalProperties": false,
  "properties": {
    "$schema": { "type": "string" },
    "tenant_id": {
      "type": "string",
      "pattern": "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
      "description": "Entra tenant UUID. Public OAuth identifier (not a secret)."
    },
    "client_id": {
      "type": "string",
      "pattern": "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
      "description": "Entra app registration client ID. Public OAuth identifier."
    },
    "app_id_uri": {
      "type": "string",
      "pattern": "^api://[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
      "description": "Application ID URI exposed under 'Expose an API' in Entra."
    },
    "issuer": {
      "type": "string",
      "pattern": "^https://login\\.microsoftonline\\.com/[0-9a-fA-F-]+/v2\\.0$",
      "description": "Entra v2.0 issuer URL. Matches the iss claim PyJWT validates against."
    },
    "scope_name": {
      "type": "string",
      "pattern": "^[A-Za-z][A-Za-z0-9_.-]*$",
      "description": "Single scope name exposed by the app reg (NOT prefixed with api://). Canonical default is 'access_as_user'."
    },
    "token_version": {
      "type": "string",
      "enum": ["2.0"],
      "description": "JWT 'ver' claim contract. We only accept v2.0 tokens; v1.0 is rejected (different host + claim shape)."
    },
    "env_var_names": {
      "type": "object",
      "description": "Canonical env-var names per identity field. Gate 3 uses this to discover references across config files.",
      "required": ["tenant_id", "client_id"],
      "additionalProperties": false,
      "properties": {
        "tenant_id": {
          "type": "array",
          "items": { "type": "string" },
          "minItems": 1
        },
        "client_id": {
          "type": "array",
          "items": { "type": "string" },
          "minItems": 1
        }
      }
    },
    "frontend_env_prefix": {
      "type": "string",
      "description": "Prefix used by Next.js for client-bundled env vars (so NEXT_PUBLIC_AZURE_TENANT_ID is recognised as the tenant_id variant)."
    }
  }
}
```

- [ ] **Step 1.2 — Write `identity-contract.json`**

  > **NOTE TO IMPLEMENTER:** The placeholder `00000000-0000-0000-0000-000000000000` values are deliberately used here — they match the placeholder pattern already in `.github/workflows/ci.yml`. Pablo replaces them with the actual production tenant/client GUIDs on a follow-up commit on this same branch (BEFORE Task 11's negative-control demo, so Gate 3 has a real baseline to lint against). The schema enforces GUID shape, so a typo fails fast.

```json
{
  "$schema": "./identity-contract.schema.json",
  "tenant_id": "00000000-0000-0000-0000-000000000000",
  "client_id": "00000000-0000-0000-0000-000000000000",
  "app_id_uri": "api://00000000-0000-0000-0000-000000000000",
  "issuer": "https://login.microsoftonline.com/00000000-0000-0000-0000-000000000000/v2.0",
  "scope_name": "access_as_user",
  "token_version": "2.0",
  "frontend_env_prefix": "NEXT_PUBLIC_",
  "env_var_names": {
    "tenant_id": [
      "AZURE_TENANT_ID",
      "JWT_TENANT_ID",
      "NEXT_PUBLIC_AZURE_TENANT_ID"
    ],
    "client_id": [
      "AZURE_CLIENT_ID",
      "JWT_CLIENT_ID",
      "NEXT_PUBLIC_AZURE_CLIENT_ID"
    ]
  }
}
```

- [ ] **Step 1.3 — Write `identity-contract.README.md`**

```markdown
# Identity Contract

`identity-contract.json` is the **single source of truth** for MSAI v2's Entra ID identity wiring.

## What it contains

Public OAuth identifiers: tenant ID, client ID, app ID URI, issuer URL, scope name, token version, and the canonical env-var names that reference them.

## What it does NOT contain

Secrets. The contract is committed to git. Real secrets (passwords, signing keys, API keys, refresh tokens) live in `.env` (gitignored) or GitHub Actions secrets.

> **Why tenant/client IDs are not secrets:** they're public OAuth identifiers that ship in every browser's MSAL JS bundle and in the issuer URL of every Entra-issued JWT. Knowing them does not grant any access.

## Who reads it

- **Gate 1** (`frontend/eslint-rules/msal-scopes.mjs`) — validates MSAL scope literals against `app_id_uri + scope_name`
- **Gate 2** (`backend/tests/integration/auth_gate/test_auth_contract.py`) — mints positive-case tokens with `aud=client_id`, `iss=issuer`, `scp=scope_name`, `ver=token_version`
- **Gate 3** (`backend/tests/integration/auth_gate/test_identity_contract.py`) — file-walker that asserts every `AZURE_TENANT_ID`/`AZURE_CLIENT_ID` reference in `.env.example`, compose, and workflow files matches the contract

## Runtime consumption

**None.** Runtime config flows through env vars (pydantic-settings on the backend, `NEXT_PUBLIC_*` on the frontend). The contract is enforced only at lint time and test time.

## Updating the contract

If you need to rotate the tenant or client ID (e.g., new app registration, separate test tenant for the multi-account-fleet initiative):

1. Update `identity-contract.json` with the new values
2. Update `.env` / GH Actions secrets / Azure config to match
3. Run `cd backend && uv run pytest tests/integration/auth_gate/test_identity_contract.py -v` locally — should pass once everything is consistent
4. Open PR. CI Gate 3 enforces the contract automatically

## Future: multi-tenant arrays

If MSAI ever needs to operate against multiple Entra tenants concurrently (e.g., per-customer tenants in the broker-fleet initiative), the schema will grow to accept arrays of contracts. The current v1 shape covers single-tenant deployment.
```

- [ ] **Step 1.4 — Validate the contract against the schema locally**

Run from worktree root:

```bash
python3 -c "import json, sys; schema=json.load(open('identity-contract.schema.json')); contract=json.load(open('identity-contract.json')); from jsonschema import validate; validate(contract, schema); print('OK')"
```

Expected: `OK`. If `jsonschema` is missing, install via `uv pip install jsonschema` in the worktree's backend env.

- [ ] **Step 1.5 — Commit Task 1**

```bash
git add identity-contract.json identity-contract.schema.json identity-contract.README.md
git commit -m "feat(auth-gate): add identity-contract.json single source of truth"
```

---

## Task 2 — Extend `validate_token` to enforce `scp` and `ver`

**Goal:** Backend now rejects tokens missing `scp` claim, tokens with wrong `scp` value, and tokens with `ver != "2.0"`. Defense-in-depth + enables Gate 2's negative cases 3, 4, 8, 10.

**Files:**

- Modify: `backend/src/msai/core/auth.py` (add module constants + extend `validate_token` body)
- Modify: `backend/tests/unit/test_auth.py` (update `_make_token` helper to include `scp` + `ver` by default; add new tests for `scp`/`ver` enforcement)

**Approach:** Strict TDD. Add new failing tests first; they fail because today's validator doesn't enforce. Then add the enforcement code; they pass.

- [ ] **Step 2.1 — Read the existing `_make_token` helper in `backend/tests/unit/test_auth.py`**

Run:

```bash
grep -n "_make_token\|def _" backend/tests/unit/test_auth.py | head -20
```

Note the line numbers of `_make_token` (the JWT mint helper) and any test that uses it without `scp`/`ver`. You'll modify it in Step 2.4.

- [ ] **Step 2.2 — Add three new failing tests to `backend/tests/unit/test_auth.py`**

The existing helper signature is (verified by Step 2.1):

```python
def _make_token(
    private_key: rsa.RSAPrivateKey,
    *,
    sub: str = "user-123",
    aud: str = CLIENT_ID,
    iss: str = ISSUER,
    exp_offset: int = 3600,
    extra_claims: dict[str, Any] | None = None,
) -> str:
```

So the new tests pass `extra_claims` to override / add claims. Append at the end of the file. These FAIL today because `validate_token` does not enforce these claims yet.

```python
def test_validate_token_rejects_missing_scp_claim(rsa_keypair) -> None:
    """Defense-in-depth: a token without scp must be rejected even with correct aud/iss/exp/sub."""
    private_key, public_pem = rsa_keypair
    # _validator_with_public_key: build validator with stubbed JWKS
    # (see helper notes below).
    validator = _validator_with_public_key(public_pem)
    # extra_claims={"scp": None} REMOVES the claim per the new helper semantic
    # (Step 2.4). Payload has NO scp claim → MissingRequiredClaimError from PyJWT.
    token = _make_token(private_key, extra_claims={"scp": None})
    with pytest.raises(pyjwt.MissingRequiredClaimError):
        validator.validate_token(token)


def test_validate_token_rejects_wrong_scp_value(rsa_keypair) -> None:
    """A token with scp=User.Read (right shape, wrong content) must be rejected."""
    private_key, public_pem = rsa_keypair
    validator = _validator_with_public_key(public_pem)
    token = _make_token(private_key, extra_claims={"scp": "User.Read"})
    with pytest.raises(pyjwt.InvalidTokenError, match="Required scope 'access_as_user' missing"):
        validator.validate_token(token)


def test_validate_token_rejects_wrong_token_version(rsa_keypair) -> None:
    """A token with ver=1.0 must be rejected even if all other claims are correct."""
    private_key, public_pem = rsa_keypair
    validator = _validator_with_public_key(public_pem)
    token = _make_token(private_key, extra_claims={"ver": "1.0"})
    with pytest.raises(pyjwt.InvalidTokenError, match="Token version mismatch"):
        validator.validate_token(token)
```

> **Note on `_validator_with_public_key`:** if the existing test file doesn't already define this helper (or an equivalent), reuse the existing pattern in `tests/unit/test_auth.py` for constructing a validator with a mocked JWKS. The current file constructs an `EntraIDValidator` and assigns `_jwks_client = MagicMock(...)`. Inline that same pattern in each test or extract to a fixture — match the file's existing style.

- [ ] **Step 2.3 — Run the new tests and verify they FAIL**

Run from `backend/`:

```bash
cd backend && uv run pytest tests/unit/test_auth.py::test_validate_token_rejects_missing_scp_claim tests/unit/test_auth.py::test_validate_token_rejects_wrong_scp_value tests/unit/test_auth.py::test_validate_token_rejects_wrong_token_version -v
```

Expected: 3 FAILED. The first two will likely show "DID NOT RAISE" because the validator returns successfully. The third will show "DID NOT RAISE" likewise.

- [ ] **Step 2.4 — Extend `_make_token` defaults to include `scp` and `ver`, with explicit "remove on None" semantic in `extra_claims`**

The existing helper already accepts `extra_claims: dict[str, Any] | None = None`. Two surgical changes:

1. **Add `scp` and `ver` to the base payload** (so every existing test that calls `_make_token(private_key)` without override gets canonical delegated-token defaults).
2. **Update the `extra_claims` merge** so `{"scp": None}` REMOVES the claim, while `{"scp": "User.Read"}` overrides. (Existing `payload.update(extra_claims)` would assign `None` to `scp` — a token with `"scp": null` is not the same as a token MISSING `scp` for PyJWT's `require=["scp"]` check.)

Replace the existing body (preserve the function signature, just modify the payload construction):

```python
def _make_token(
    private_key: rsa.RSAPrivateKey,
    *,
    sub: str = "user-123",
    aud: str = CLIENT_ID,
    iss: str = ISSUER,
    exp_offset: int = 3600,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Encode a JWT signed with the given RSA private key."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "aud": aud,
        "iss": iss,
        "iat": now,
        "exp": now + exp_offset,
        # NEW canonical delegated-token defaults (Task 2 extension):
        "scp": "access_as_user",
        "ver": "2.0",
    }
    # extra_claims: value of None REMOVES the claim entirely (negative-case
    # support); any other value overrides. This is the cleanest way to test
    # "missing scp" — `payload.update({"scp": None})` would assign null,
    # which PyJWT's require=["scp"] does NOT treat as missing.
    if extra_claims:
        for k, v in extra_claims.items():
            if v is None:
                payload.pop(k, None)
            else:
                payload[k] = v

    private_pem = _private_key_to_pem(private_key)
    return pyjwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": "test-key-1"})
```

Now `_make_token(private_key, extra_claims={"scp": None})` correctly omits the claim from the payload (Step 2.2's `test_validate_token_rejects_missing_scp_claim`).

- [ ] **Step 2.5 — Re-run the existing tests in `test_auth.py` to confirm they still pass with the new helper defaults**

```bash
cd backend && uv run pytest tests/unit/test_auth.py -v -k "not rejects_missing_scp and not rejects_wrong_scp and not rejects_wrong_token_version"
```

Expected: all existing tests PASS. The three new tests still FAIL (validator isn't extended yet).

- [ ] **Step 2.6 — Extend `validate_token` in `backend/src/msai/core/auth.py`**

At the top of the file (after the imports, before the class), add the module constants:

```python
# Expected delegated-token claim values (single source of truth in identity-contract.json;
# duplicated here as constants to avoid a runtime JSON load in the auth hot path).
_EXPECTED_SCOPE = "access_as_user"
_EXPECTED_TOKEN_VERSION = "2.0"
```

Then replace the existing `validate_token` method body (currently lines ~46-56) with:

```python
def validate_token(self, token: str) -> dict[str, Any]:
    # PyJWKClient raises PyJWKClientError on JWKS lookup misses (bad kid,
    # unreachable JWKS). PyJWKClientError is NOT a subclass of
    # jwt.InvalidTokenError, so without this wrap get_current_user's
    # `except jwt.InvalidTokenError` clause misses it and FastAPI returns 500
    # instead of 401. Codex P1 from plan-review iter 1.
    try:
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
    except jwt.PyJWKClientError as exc:
        raise jwt.InvalidTokenError(f"Signing key not found in JWKS: {exc}") from exc

    payload: dict[str, Any] = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=self._audience,
        issuer=self._issuer,
        options={"require": ["exp", "iss", "aud", "sub", "scp"]},
    )

    # scp is a space-separated string per Entra v2.0 spec.
    scp_value: str = payload.get("scp", "")
    if _EXPECTED_SCOPE not in scp_value.split():
        raise jwt.InvalidTokenError(
            f"Required scope '{_EXPECTED_SCOPE}' missing from scp claim "
            f"(got: '{scp_value}')"
        )

    ver_value = payload.get("ver")
    if ver_value != _EXPECTED_TOKEN_VERSION:
        raise jwt.InvalidTokenError(
            f"Token version mismatch: expected '{_EXPECTED_TOKEN_VERSION}', "
            f"got '{ver_value}'"
        )

    return payload
```

- [ ] **Step 2.7 — Run the new tests and verify they PASS**

```bash
cd backend && uv run pytest tests/unit/test_auth.py::test_validate_token_rejects_missing_scp_claim tests/unit/test_auth.py::test_validate_token_rejects_wrong_scp_value tests/unit/test_auth.py::test_validate_token_rejects_wrong_token_version -v
```

Expected: 3 PASSED.

- [ ] **Step 2.8 — Run ALL backend unit tests to catch ripple**

```bash
cd backend && uv run pytest tests/unit/ -v
```

Expected: ALL PASS. The `_make_token` defaults now include `scp` and `ver`, so every existing test mints a valid token. If any test fails, it likely overrides `_make_token` with `scp=None` or `ver=None` somewhere — find it and either fix the test's mint or split the overrides.

- [ ] **Step 2.9 — Run ruff + mypy on the changes**

```bash
cd backend && uv run ruff check src/msai/core/auth.py tests/unit/test_auth.py
cd backend && uv run mypy src/msai/core/auth.py --strict
```

Expected: no errors. If mypy complains about the `scp_value.split()` chain returning `Any`, type-annotate explicitly:

```python
scp_value: str = payload.get("scp", "")
```

- [ ] **Step 2.10 — Commit Task 2**

```bash
git add backend/src/msai/core/auth.py backend/tests/unit/test_auth.py
git commit -m "feat(auth): enforce scp=access_as_user and ver=2.0 in validate_token"
```

---

## Task 3 — Integration test scaffolding (`tests/integration/auth_gate/conftest.py`)

**Goal:** Shared fixtures for Gate 2 and Gate 3 — network-block autouse, JWKS keypair, token factory, dependency-override clearance.

**Files:**

- Create: `backend/tests/integration/auth_gate/__init__.py` (empty marker)
- Create: `backend/tests/integration/auth_gate/conftest.py`

- [ ] **Step 3.1 — Create the package marker**

```bash
touch backend/tests/integration/auth_gate/__init__.py
```

- [ ] **Step 3.2 — Write `backend/tests/integration/auth_gate/conftest.py`**

```python
"""Shared fixtures for integration-level auth gate tests.

These tests run WITHOUT network access (verified by the autouse block_network
fixture) AND without the autouse get_current_user override from the parent
conftest.py (which is fine for unit tests but would silently bypass every
negative case in Gate 2).
"""

from __future__ import annotations

import json
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from msai.core.auth import EntraIDValidator, get_current_user
from msai.main import app


# ---------------------------------------------------------------------------
# 1) Identity contract — single source of truth for token claims.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def identity_contract() -> dict[str, Any]:
    """Load the committed identity-contract.json (relative to repo root)."""
    # conftest.py lives at backend/tests/integration/auth_gate/conftest.py
    # → repo root is parents[4]. The `parents[4]` chain:
    #   parents[0] = .../auth_gate
    #   parents[1] = .../integration
    #   parents[2] = .../tests
    #   parents[3] = .../backend
    #   parents[4] = repo root (where identity-contract.json lives)
    repo_root = Path(__file__).resolve().parents[4]
    contract_path = repo_root / "identity-contract.json"
    with contract_path.open() as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 2) Network block — assert no live calls during the gate.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that attempts a real network call.

    The gate must run offline. JWKS lookups go through the patched mock client
    injected on the validator instance — not through Microsoft's discovery URL.
    """
    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            "Network call blocked in integration auth-gate test. "
            "Use the mocked JWKS client; do not hit live Entra."
        )

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    monkeypatch.setattr(httpx, "get", _raise)
    # Block AsyncClient.send (covers ASGITransport-internal calls? No — ASGITransport
    # routes to the in-process app and does NOT call .send on a real transport.
    # We only patch the SYNC paths so external libs (PyJWKClient.fetch_jwks) raise).


# ---------------------------------------------------------------------------
# 3) Clear the unit-test autouse override so the REAL dependency runs.
#    Function-scoped + autouse + depends on the parent `_override_auth`
#    fixture (which is itself function-scoped autouse at backend/tests/
#    conftest.py:49). Pytest runs the parent fixture FIRST (installs the
#    override), then runs this fixture (pops the override), then runs the
#    test (real validator fires). This is Codex P1 plan-review iter 1 —
#    a module-scoped pop runs ONCE before the parent has a chance to
#    re-install per test, so the override would still leak.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_unit_auth_override(_override_auth: None) -> Generator[None, None, None]:
    """Pop the parent's get_current_user override AFTER it's been installed.

    The dependency on ``_override_auth`` ensures pytest runs the parent
    fixture first (installs the dict entry). We then pop the dict entry,
    so the test body runs against the REAL get_current_user dependency.
    Pytest's teardown order is reverse-LIFO, so the parent fixture's
    own teardown still cleans up after us if it tries.
    """
    from msai.core.auth import get_current_user as _get_current_user
    app.dependency_overrides.pop(_get_current_user, None)
    yield


# ---------------------------------------------------------------------------
# 4) Test JWKS — two RSA keypairs (current key + rotated-out key).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def jwks_keypairs() -> dict[str, dict[str, Any]]:
    """Generate two RSA-2048 keypairs (current + rotated). Session-scoped to
    amortise the ~50ms RSA keygen cost across all tests in the module."""
    keypairs = {}
    for kid in ("test-kid-current", "test-kid-rotated"):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        keypairs[kid] = {
            "private_pem": private_pem,
            "public_pem": public_pem,
            "private_key": private_key,
        }
    return keypairs


# ---------------------------------------------------------------------------
# 5) Mock JWKS client — returns signing keys by kid; raises on unknown kid.
# ---------------------------------------------------------------------------

class _MockSigningKey:
    """Stand-in for jwt.PyJWK with the .key attribute PyJWT consumes."""

    def __init__(self, public_pem: bytes) -> None:
        self.key = public_pem


class _MockJWKSClient:
    """Mock of PyJWKClient that resolves test JWKS without network access."""

    def __init__(self, keypairs: dict[str, dict[str, Any]]) -> None:
        self._keys = {kid: _MockSigningKey(kp["public_pem"]) for kid, kp in keypairs.items()}

    def get_signing_key_from_jwt(self, token: str) -> _MockSigningKey:
        # Decode header unsafely to extract kid
        header = jwt.get_unverified_header(token)
        kid = header.get("kid", "")
        if kid not in self._keys:
            raise jwt.PyJWKClientError(f"Unable to find a signing key matching kid={kid!r}")
        return self._keys[kid]


@pytest.fixture(scope="session")
def mock_jwks_client(jwks_keypairs: dict[str, dict[str, Any]]) -> _MockJWKSClient:
    return _MockJWKSClient(jwks_keypairs)


# ---------------------------------------------------------------------------
# 6) Real EntraIDValidator with the JWKS client swapped out.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def validator(
    identity_contract: dict[str, Any],
    mock_jwks_client: _MockJWKSClient,
) -> EntraIDValidator:
    """Construct a real EntraIDValidator, then replace its JWKS client.

    The constructor builds a real PyJWKClient (which does NOT fetch on
    construction), but we replace it immediately so no test can hit live Entra.
    """
    v = EntraIDValidator(
        tenant_id=identity_contract["tenant_id"],
        client_id=identity_contract["client_id"],
    )
    v._jwks_client = mock_jwks_client  # type: ignore[assignment]
    return v


@pytest.fixture(scope="module", autouse=True)
def _init_module_validator(validator: EntraIDValidator) -> Generator[None, None, None]:
    """Install the module's validator into the auth module's global slot so
    get_current_user finds it via get_validator()."""
    from msai.core import auth as auth_module

    prior = auth_module._validator
    auth_module._validator = validator
    try:
        yield
    finally:
        auth_module._validator = prior


# ---------------------------------------------------------------------------
# 7) Token factory — mints valid + invalid tokens for parametrised cases.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def make_token(identity_contract: dict[str, Any], jwks_keypairs: dict[str, dict[str, Any]]):
    """Return a token-factory that produces signed JWTs for parametrised cases.

    Returns ``make_token(**overrides)`` — every claim has a sensible canonical
    delegated-user default; pass overrides to test specific negative cases.
    """

    # Codex P2 plan-review iter 2: read scp/ver defaults from the contract,
    # not hardcoded strings. If the contract's scope_name or token_version
    # ever changes, the factory automatically follows and the test stays
    # consistent with what Gate 1 and the backend validator enforce.
    default_scp = identity_contract["scope_name"]
    default_ver = identity_contract["token_version"]

    def _factory(
        *,
        aud: str | None = ...,  # type: ignore[assignment]
        iss: str | None = ...,  # type: ignore[assignment]
        scp: str | None = ...,  # type: ignore[assignment]
        ver: str | None = ...,  # type: ignore[assignment]
        exp_delta_seconds: int = 3600,
        sub: str = "00000000-0000-0000-0000-000000000001",
        roles: list[str] | None = None,
        azp: str | None = ...,  # type: ignore[assignment]
        kid: str = "test-kid-current",
        sign_with_kid: str = "test-kid-current",
    ) -> str:
        if aud is ...:
            aud = identity_contract["client_id"]
        if iss is ...:
            iss = identity_contract["issuer"]
        if scp is ...:
            scp = default_scp
        if ver is ...:
            ver = default_ver
        if azp is ...:
            azp = identity_contract["client_id"]

        now = int(time.time())
        payload: dict[str, Any] = {
            "aud": aud,
            "iss": iss,
            "iat": now,
            "nbf": now,
            "exp": now + exp_delta_seconds,
            "sub": sub,
            "azp": azp,
            "azpacr": "0",
        }
        if scp is not None:
            payload["scp"] = scp
        if ver is not None:
            payload["ver"] = ver
        if roles is not None:
            payload["roles"] = roles

        sign_key = jwks_keypairs[sign_with_kid]["private_pem"]
        return jwt.encode(
            payload, sign_key, algorithm="RS256", headers={"kid": kid}
        )

    return _factory


# ---------------------------------------------------------------------------
# 8) Async HTTP client through ASGITransport — hits the real FastAPI app.
# ---------------------------------------------------------------------------

@pytest.fixture
async def client(_init_module_validator: None) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")
```

- [ ] **Step 3.3 — Verify conftest loads without errors**

```bash
cd backend && uv run pytest tests/integration/auth_gate/ --collect-only -q
```

Expected: collects with no errors. (No test files yet, so the output may say "no tests ran" — that's fine, we just want the conftest to import cleanly.)

- [ ] **Step 3.4 — Commit Task 3**

```bash
git add backend/tests/integration/auth_gate/__init__.py backend/tests/integration/auth_gate/conftest.py
git commit -m "test(auth-gate): scaffold integration conftest (JWKS, token factory, network block)"
```

---

## Task 4 — Gate 2: `test_auth_contract.py` (11 cases)

**Goal:** 1 positive case + 10 negative cases parametrized; smoke test ensures the autouse override was actually cleared.

**Files:**

- Create: `backend/tests/integration/auth_gate/test_auth_contract.py`

- [ ] **Step 4.1 — Write the test file**

```python
"""Gate 2 — backend auth middleware contract test.

Each case mints a synthetic JWT with one specific manipulation, then asserts
BOTH layers:
- Layer A: validator.validate_token raises the SPECIFIC PyJWT subclass
- Layer B: GET /api/v1/account/health returns the correct HTTP status

No network calls (autouse _block_network fixture); no unit-test override
(autouse _clear_unit_auth_override fixture).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
import pytest

from msai.core.auth import EntraIDValidator

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Smoke test — FIRST in module so a missing override-clearance fails loudly.
# ---------------------------------------------------------------------------

async def test_unit_autouse_override_is_cleared(client: httpx.AsyncClient) -> None:
    """If the autouse get_current_user override from tests/conftest.py leaks
    into this module, every other test silently passes because the real
    validator never fires. This test asserts the override is cleared."""
    resp = await client.get("/api/v1/account/health")
    # Without Authorization header AND without X-API-Key, the real dependency
    # raises 401 "Missing Authorization header or X-API-Key".
    assert resp.status_code == 401
    assert "Missing Authorization" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Parametrised contract cases.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthCase:
    id: str
    overrides: dict[str, Any]
    expected_exception: type[BaseException] | None
    expected_status: int
    detail_substring: str | None = None


AUTH_CASES: list[AuthCase] = [
    # Positive
    AuthCase(
        id="positive",
        overrides={},
        expected_exception=None,
        expected_status=200,
    ),
    # 1. Graph audience (the literal May 20 bug)
    AuthCase(
        id="graph_audience",
        overrides={"aud": "00000003-0000-0000-c000-000000000000"},
        expected_exception=jwt.InvalidAudienceError,
        expected_status=401,
        detail_substring="Invalid token",
    ),
    # 2. Wrong tenant in issuer
    AuthCase(
        id="wrong_tenant_in_issuer",
        overrides={
            "iss": (
                "https://login.microsoftonline.com/"
                "ffffffff-ffff-ffff-ffff-ffffffffffff/v2.0"
            )
        },
        expected_exception=jwt.InvalidIssuerError,
        expected_status=401,
    ),
    # 3. Missing scp claim
    AuthCase(
        id="missing_scp",
        overrides={"scp": None},
        expected_exception=jwt.MissingRequiredClaimError,
        expected_status=401,
    ),
    # 4. Wrong scp value (scp=User.Read)
    AuthCase(
        id="wrong_scp_user_read",
        overrides={"scp": "User.Read"},
        expected_exception=jwt.InvalidTokenError,
        expected_status=401,
        detail_substring="Required scope 'access_as_user' missing",
    ),
    # 5. Expired token
    AuthCase(
        id="expired",
        overrides={"exp_delta_seconds": -10},
        expected_exception=jwt.ExpiredSignatureError,
        expected_status=401,
    ),
    # 6. Bad kid (JWKS lookup miss) — wrapped in InvalidTokenError by
    #    validate_token (see auth.py change in Task 2.6 wrap-block) so that
    #    get_current_user's `except jwt.InvalidTokenError` correctly surfaces
    #    a 401 (not 500).
    AuthCase(
        id="bad_kid",
        overrides={"kid": "unknown-kid", "sign_with_kid": "test-kid-current"},
        expected_exception=jwt.InvalidTokenError,
        expected_status=401,
        detail_substring="Signing key not found",
    ),
    # 7. Wrong signature (signed with rotated key but claims current kid)
    AuthCase(
        id="wrong_signature",
        overrides={"kid": "test-kid-current", "sign_with_kid": "test-kid-rotated"},
        expected_exception=jwt.InvalidSignatureError,
        expected_status=401,
    ),
    # 8. App-only roles claim with no scp
    AuthCase(
        id="app_only_roles_no_scp",
        overrides={"roles": ["api.access"], "scp": None},
        expected_exception=jwt.MissingRequiredClaimError,
        expected_status=401,
    ),
    # 9. v1.0 issuer host (different domain)
    AuthCase(
        id="v1_issuer_host",
        overrides={"iss": "https://sts.windows.net/00000000-0000-0000-0000-000000000000/"},
        expected_exception=jwt.InvalidIssuerError,
        expected_status=401,
    ),
    # 10. ver=1.0 (with v2.0-shaped issuer)
    AuthCase(
        id="ver_1_0",
        overrides={"ver": "1.0"},
        expected_exception=jwt.InvalidTokenError,
        expected_status=401,
        detail_substring="Token version mismatch",
    ),
]


@pytest.mark.parametrize("case", AUTH_CASES, ids=lambda c: c.id)
async def test_auth_contract(
    client: httpx.AsyncClient,
    validator: EntraIDValidator,
    make_token: Callable[..., str],
    identity_contract: dict[str, Any],
    case: AuthCase,
) -> None:
    token = make_token(**case.overrides)

    # Layer A — validator raises the specific exception subclass
    if case.expected_exception is not None:
        with pytest.raises(case.expected_exception):
            validator.validate_token(token)
    else:
        # Positive — must NOT raise. Compare against contract values (NOT
        # hardcoded "access_as_user"/"2.0") so a drift between the contract
        # and the backend constants in auth.py surfaces here. Codex P2
        # plan-review iter 2.
        payload = validator.validate_token(token)
        assert payload["scp"] == identity_contract["scope_name"]
        assert payload["ver"] == identity_contract["token_version"]

    # Layer B — FastAPI surfaces the right HTTP status
    async with client as ac:
        resp = await ac.get(
            "/api/v1/account/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == case.expected_status, (
        f"case={case.id}: expected {case.expected_status}, got {resp.status_code}: {resp.text}"
    )
    if case.detail_substring is not None and case.expected_status == 401:
        detail = resp.json()["detail"]
        assert case.detail_substring in detail, (
            f"case={case.id}: expected detail substring "
            f"{case.detail_substring!r} in {detail!r}"
        )


# ---------------------------------------------------------------------------
# Coverage assertions — second authenticated endpoint (PRD US-001 ACs).
# ---------------------------------------------------------------------------

async def test_auth_dependency_covers_other_router(
    client: httpx.AsyncClient,
) -> None:
    """Confirm an authenticated endpoint OUTSIDE the account router also
    enforces auth — catches the 'new router shipped without
    Depends(get_current_user)' regression class (PRD US-001 acceptance).

    Picks `/api/v1/system/health` (different router, in
    backend/src/msai/api/system.py). Codex P2 plan-review iter 2:
    /api/v1/system/health performs DB + Redis probes on the AUTHENTICATED
    code path, so we cannot rely on the offline gate to pass a 2xx
    assertion without mocking those probes. The auth-wiring regression
    class is fully covered by the 401-without-auth check alone — auth
    runs BEFORE the handler body, so a missing dependency surfaces as
    a non-401 status (200 or 422) here.
    """
    async with client as ac:
        unauth = await ac.get("/api/v1/system/health")
        assert unauth.status_code == 401, (
            f"Auth dependency not wired on /api/v1/system/health: "
            f"expected 401, got {unauth.status_code}"
        )
```

- [ ] **Step 4.2 — Run the file**

```bash
cd backend && uv run pytest tests/integration/auth_gate/test_auth_contract.py -v
```

Expected: All cases PASS. The smoke test passes (override is cleared by the new conftest), the 11 parametrized cases pass (validator from Task 2 + middleware from existing `auth.py:get_current_user`), and the second-endpoint coverage test passes.

If any case fails:

- `positive` failing → check the validator's audience/issuer/scp/ver match the contract values
- A negative failing as `DID NOT RAISE` → the validator extension in Task 2 wasn't applied; re-check `auth.py:46-85` for the required claims and the explicit `scp`/`ver` checks
- A negative failing with the WRONG exception class → PyJWT may raise a parent class instead of the specific subclass; tighten the `pytest.raises(...)` to match what's actually raised, OR adjust the validator to raise the more specific subclass

- [ ] **Step 4.3 — Run ruff + mypy on the new test file**

```bash
cd backend && uv run ruff check tests/integration/auth_gate/test_auth_contract.py
cd backend && uv run mypy tests/integration/auth_gate/test_auth_contract.py --strict
```

Expected: no errors.

- [ ] **Step 4.4 — Commit Task 4**

```bash
git add backend/tests/integration/auth_gate/test_auth_contract.py
git commit -m "test(auth-gate): add Gate 2 — JWT middleware contract test (11 cases)"
```

---

## Task 5 — Gate 3: `test_identity_contract.py` (cross-file consistency)

**Goal:** Schema-validate `identity-contract.json`, then walk `.env.example` / compose / workflow files asserting no drift.

**Files:**

- Create: `backend/tests/integration/auth_gate/test_identity_contract.py`
- Modify: `backend/pyproject.toml` (add `jsonschema` to dev deps if not already present)

- [ ] **Step 5.1 — Check whether `jsonschema` is already in dev deps**

```bash
grep -A 3 "jsonschema" backend/pyproject.toml
```

If present: no action. If not: add `"jsonschema>=4.0.0"` to the **`dev`** extras (the only optional-dependencies key in `backend/pyproject.toml`):

```toml
# Inside [project.optional-dependencies]
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
    # ... existing entries ...
    "jsonschema>=4.0.0",  # NEW
]
```

Then sync: `cd backend && uv sync --extra dev`.

- [ ] **Step 5.2 — Write `backend/tests/integration/auth_gate/test_identity_contract.py`**

```python
"""Gate 3 — cross-file consistency lint for identity-contract.json.

For each canonical env-var declared in identity-contract.json#env_var_names,
walks the repo for references and asserts every literal value matches the
contract (or is a documented placeholder).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import jsonschema
import pytest


# Dirs we never scan — generated, vendored, or our own contract.
# IMPORTANT: matched against path.relative_to(root).parts so this works
# both from the canonical repo root AND from a worktree under .worktrees/
# (where every absolute path contains .worktrees and would otherwise skip
# the entire repo — that's Codex's P0 finding on the plan-review loop).
EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "data",
    ".next",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    # NOTE: do NOT include ".worktrees" here. The repo_root resolution finds
    # identity-contract.json in the worktree directly, so the walker scans
    # ONLY files inside that worktree's tree (relative paths). Adding
    # ".worktrees" would skip every file inside the worktree itself.
}
EXCLUDE_FILES = {
    "identity-contract.json",
    "identity-contract.schema.json",
    "identity-contract.README.md",
}

# A value matches the placeholder convention if it starts with any of these prefixes.
PLACEHOLDER_PREFIXES = ("your-", "<", "TBD", "EXAMPLE", "REPLACE", "placeholder")
# A value is "the all-zero placeholder GUID" — explicitly allowed (matches ci.yml).
ALL_ZERO_GUID = "00000000-0000-0000-0000-000000000000"

# Regex for a typical env-var line like `KEY=value` or `KEY: value` or `KEY: "value"`.
ENV_LINE_RE = re.compile(
    r"^\s*(?P<key>[A-Z_][A-Z0-9_]+)\s*[:=]\s*[\"']?(?P<value>[^\"'\s#$]+)[\"']?",
    re.MULTILINE,
)


@dataclass
class Drift:
    file: Path
    line: int
    key: str
    actual: str
    expected: str

    def __str__(self) -> str:
        return (
            f"{self.file}:{self.line}: drift — "
            f"{self.key}={self.actual!r}, expected {self.expected!r}"
        )


# ---------------------------------------------------------------------------
# Repo discovery helpers.
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """Resolve the repo root (the dir containing identity-contract.json)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "identity-contract.json").exists():
            return parent
    raise RuntimeError("identity-contract.json not found in any parent directory")


def _iter_target_files(root: Path) -> Iterator[Path]:
    """Yield all files Gate 3 inspects.

    Path-component matching uses relative-to-root parts so the walker works
    both from the canonical repo root AND from a worktree under
    `.worktrees/<name>/` (where absolute path.parts would always contain
    `.worktrees` and incorrectly skip the entire scan — Codex's P0 finding).
    """
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Use RELATIVE parts so worktree-root detection works correctly.
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in EXCLUDE_DIRS for part in rel_parts):
            continue
        if path.name in EXCLUDE_FILES:
            continue
        if path.name == ".env":
            continue  # gitignored real values
        if (
            path.name.endswith(".env.example")
            or path.name == ".env.example"
            or (
                path.suffix in (".yml", ".yaml")
                and ("docker-compose" in path.name or "workflows" in rel_parts)
            )
        ):
            yield path


# ---------------------------------------------------------------------------
# Test 1 — Schema validates the contract.
# ---------------------------------------------------------------------------

def test_identity_contract_matches_schema() -> None:
    root = _repo_root()
    schema = json.loads((root / "identity-contract.schema.json").read_text())
    contract = json.loads((root / "identity-contract.json").read_text())

    jsonschema.validate(contract, schema)


# ---------------------------------------------------------------------------
# Test 2 — Cross-file drift detection on the real repo.
# ---------------------------------------------------------------------------

def test_no_drift_across_repo_files() -> None:
    root = _repo_root()
    contract = json.loads((root / "identity-contract.json").read_text())
    expected = {
        env_var: contract["tenant_id"]
        for env_var in contract["env_var_names"]["tenant_id"]
    }
    expected.update(
        {env_var: contract["client_id"] for env_var in contract["env_var_names"]["client_id"]}
    )

    drifts: list[Drift] = []
    files_scanned = 0
    for file in _iter_target_files(root):
        files_scanned += 1
        text = file.read_text(errors="replace")
        for match in ENV_LINE_RE.finditer(text):
            key = match.group("key")
            value = match.group("value")
            if key not in expected:
                continue
            if not value or value == ALL_ZERO_GUID:
                # Empty or all-zero placeholder — OK
                continue
            if value.startswith(PLACEHOLDER_PREFIXES):
                continue
            # Env-var passthrough (${VAR} or ${VAR:-default}) — OK
            if value.startswith("$"):
                continue
            if value != expected[key]:
                line = text[: match.start()].count("\n") + 1
                drifts.append(
                    Drift(
                        file=file,
                        line=line,
                        key=key,
                        actual=value,
                        expected=expected[key],
                    )
                )

    # Defensive: assert we actually scanned files. If files_scanned == 0 the
    # walker is silently skipping everything (Codex P0 from plan review iter 1).
    assert files_scanned > 0, (
        f"Gate 3 file walker scanned zero files from root={root}. "
        "This is the worktree-skip bug — verify _iter_target_files uses "
        "relative paths and EXCLUDE_DIRS does not include '.worktrees'."
    )

    if drifts:
        pytest.fail(
            "Identity contract drift detected:\n"
            + "\n".join(f"  {d}" for d in drifts)
            + "\n\nUpdate identity-contract.json OR the drifted file."
        )


# ---------------------------------------------------------------------------
# Test 3 — Drift IS detected when one is injected (the lint of the lint).
# ---------------------------------------------------------------------------

def test_drift_detection_works(tmp_path: Path) -> None:
    """Inject a deliberate drift in a fixture file and assert the lint catches it."""
    # Build a minimal fixture repo
    (tmp_path / "identity-contract.json").write_text(json.dumps({
        "$schema": "./identity-contract.schema.json",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "client_id": "22222222-2222-2222-2222-222222222222",
        "app_id_uri": "api://22222222-2222-2222-2222-222222222222",
        "issuer": "https://login.microsoftonline.com/11111111-1111-1111-1111-111111111111/v2.0",
        "scope_name": "access_as_user",
        "token_version": "2.0",
        "frontend_env_prefix": "NEXT_PUBLIC_",
        "env_var_names": {
            "tenant_id": ["AZURE_TENANT_ID"],
            "client_id": ["AZURE_CLIENT_ID"],
        },
    }))
    # Drift: AZURE_TENANT_ID DIFFERS from the contract above.
    (tmp_path / ".env.example").write_text(
        "AZURE_TENANT_ID=99999999-9999-9999-9999-999999999999\n"
        "AZURE_CLIENT_ID=22222222-2222-2222-2222-222222222222\n"
    )

    contract = json.loads((tmp_path / "identity-contract.json").read_text())
    expected = {
        env_var: contract["tenant_id"]
        for env_var in contract["env_var_names"]["tenant_id"]
    }
    expected.update(
        {env_var: contract["client_id"] for env_var in contract["env_var_names"]["client_id"]}
    )

    drifts: list[tuple[Path, str, str, str]] = []
    for file in tmp_path.glob("*.env.example"):
        text = file.read_text()
        for match in ENV_LINE_RE.finditer(text):
            key = match.group("key")
            value = match.group("value")
            if key not in expected:
                continue
            if not value or value == ALL_ZERO_GUID:
                continue
            if value.startswith(PLACEHOLDER_PREFIXES) or value.startswith("$"):
                continue
            if value != expected[key]:
                drifts.append((file, key, value, expected[key]))

    assert len(drifts) == 1
    file, key, actual, expected_value = drifts[0]
    assert key == "AZURE_TENANT_ID"
    assert actual == "99999999-9999-9999-9999-999999999999"
    assert expected_value == "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# Test 4 — Placeholder values are allowed.
# ---------------------------------------------------------------------------

def test_placeholder_values_in_env_example_are_allowed(tmp_path: Path) -> None:
    """`your-tenant-id`, `<GUID>`, `00000000-...` etc. should NOT be flagged."""
    (tmp_path / "fixture.env.example").write_text(
        "AZURE_TENANT_ID=your-tenant-id\n"
        "AZURE_CLIENT_ID=<GUID>\n"
        "JWT_TENANT_ID=00000000-0000-0000-0000-000000000000\n"
        "NEXT_PUBLIC_AZURE_TENANT_ID=TBD\n"
    )

    expected = {
        "AZURE_TENANT_ID": "real-tenant",
        "AZURE_CLIENT_ID": "real-client",
        "JWT_TENANT_ID": "real-tenant",
        "NEXT_PUBLIC_AZURE_TENANT_ID": "real-tenant",
    }

    drifts: list[tuple[str, str]] = []
    text = (tmp_path / "fixture.env.example").read_text()
    for match in ENV_LINE_RE.finditer(text):
        key = match.group("key")
        value = match.group("value")
        if key not in expected:
            continue
        if not value or value == ALL_ZERO_GUID:
            continue
        if value.startswith(PLACEHOLDER_PREFIXES) or value.startswith("$"):
            continue
        if value != expected[key]:
            drifts.append((key, value))

    assert drifts == []
```

- [ ] **Step 5.3 — Run the test file**

```bash
cd backend && uv run pytest tests/integration/auth_gate/test_identity_contract.py -v
```

Expected: all 4 tests PASS. If `test_no_drift_across_repo_files` fails, fix the actual drift OR document the false-positive and tighten the regex.

- [ ] **Step 5.4 — Run ruff + mypy**

```bash
cd backend && uv run ruff check tests/integration/auth_gate/test_identity_contract.py
cd backend && uv run mypy tests/integration/auth_gate/test_identity_contract.py --strict
```

Expected: no errors.

- [ ] **Step 5.5 — Commit Task 5**

```bash
git add backend/tests/integration/auth_gate/test_identity_contract.py backend/pyproject.toml
git commit -m "test(auth-gate): add Gate 3 — identity-contract.json cross-file consistency lint"
```

---

## Task 6 — Gate 1: ESLint custom rule

**Goal:** Custom ESLint rule that fails on forbidden MSAL scope literals.

**Files:**

- Create: `frontend/eslint-rules/index.mjs`
- Create: `frontend/eslint-rules/msal-scopes.mjs`
- Create: `frontend/eslint-rules/__tests__/msal-scopes.test.mjs`
- Modify: `frontend/package.json` (add devDeps)

- [ ] **Step 6.1 — Add devDependencies to `frontend/package.json`**

In the `devDependencies` block, add (or confirm they're already pinned compatibly):

```json
{
  "devDependencies": {
    "@typescript-eslint/utils": "^8.0.0",
    "@typescript-eslint/rule-tester": "^8.0.0"
  }
}
```

Then run:

```bash
cd frontend && pnpm install
```

- [ ] **Step 6.2 — Write `frontend/eslint-rules/msal-scopes.mjs`**

> **Why `.mjs` JavaScript and not `.ts`** (Codex P1 from plan-review iter 1): The frontend project doesn't ship `tsx` as a devDep, and ESLint 9's flat config loaded from `eslint.config.mjs` can't natively import `.ts` files without a TS loader. Writing the rule in plain JS with JSDoc keeps editor type hints (via `@typescript-eslint/utils` typedefs) while avoiding the entire TS-loader question. ~150 LoC, single file — JSDoc gives us 90% of the TS type rigour.

```javascript
// @ts-check
import { ESLintUtils } from "@typescript-eslint/utils";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const createRule = ESLintUtils.RuleCreator(
  (name) => `https://msai.local/eslint-rules/${name}`,
);

/**
 * @typedef {Object} IdentityContract
 * @property {string} client_id
 * @property {string} scope_name
 * @property {{ client_id: string[] }} env_var_names
 * @property {string} [frontend_env_prefix]
 */

/**
 * Find identity-contract.json by walking up from the rule's own module
 * location (NOT context.cwd, which is brittle when lint runs from an
 * editor at the repo root vs CI in `frontend/`). Codex P2 plan-review iter 1.
 *
 * @returns {IdentityContract}
 */
function loadContract() {
  const __filename = fileURLToPath(import.meta.url);
  let dir = dirname(__filename);
  const max_hops = 10;
  for (let i = 0; i < max_hops; i++) {
    const candidate = join(dir, "identity-contract.json");
    try {
      return /** @type {IdentityContract} */ (
        JSON.parse(readFileSync(candidate, "utf-8"))
      );
    } catch {
      const parent = resolve(dir, "..");
      if (parent === dir) break;
      dir = parent;
    }
  }
  throw new Error(
    `Cannot find identity-contract.json walking up from ${__filename}. ` +
      "Is this rule being loaded from outside the repo?",
  );
}

const ALLOWED_OIDC_SCOPES = new Set([
  "openid",
  "profile",
  "email",
  "offline_access",
]);

const FORBIDDEN_SUBSTRINGS = [
  "User.Read",
  "graph.microsoft.com",
  "/.default", // PR #74 anti-pattern
];

export const msalScopesMustBeApiOrOidc = createRule({
  name: "msal-scopes-must-be-api-or-oidc",
  meta: {
    type: "problem",
    docs: {
      description:
        "MSAL scope literals must be either standard OIDC (openid/profile/email/offline_access) or api://<client-id>/<scope-name> matching identity-contract.json. Forbids User.Read, graph.microsoft.com, .default — the May 20 outage class.",
    },
    schema: [],
    messages: {
      forbidden:
        'Forbidden MSAL scope literal "{{value}}" — expected "api://<client-id>/{{expectedScope}}" or one of openid/profile/email/offline_access.',
      wrong_scope_name:
        'MSAL scope name "{{actual}}" does not match identity-contract.json#scope_name ("{{expected}}").',
      unresolvable_template:
        'Cannot statically resolve MSAL scope template literal "{{raw}}". Use the canonical shape `api://${{}}NEXT_PUBLIC_AZURE_CLIENT_ID/{{expectedScope}}` where the scope name is a literal.',
    },
  },
  defaultOptions: [],
  create(context) {
    const contract = loadContract();
    const apiScopePattern = new RegExp(
      `^api://${contract.client_id}/${contract.scope_name}$`,
    );
    // Canonical env-var references that we statically trust as resolving to
    // the contract's client_id WHEN USED IN FRONTEND CODE. Filtered to vars
    // beginning with frontend_env_prefix (typically NEXT_PUBLIC_) because
    // Next.js only exposes prefixed env vars to the browser bundle — using
    // a non-prefixed backend env var here would silently resolve to undefined
    // at runtime. Codex P2 plan-review iter 2.
    const frontendPrefix = contract.frontend_env_prefix ?? "NEXT_PUBLIC_";
    const allowedClientIdEnvRefs = new Set(
      contract.env_var_names.client_id
        .filter((name) => name.startsWith(frontendPrefix))
        .map((name) => `process.env.${name}`),
    );

    function checkLiteral(node, raw) {
      if (typeof raw !== "string") return;
      if (ALLOWED_OIDC_SCOPES.has(raw)) return;
      if (apiScopePattern.test(raw)) return;

      for (const forbidden of FORBIDDEN_SUBSTRINGS) {
        if (raw.includes(forbidden)) {
          context.report({
            node,
            messageId: "forbidden",
            data: { value: raw, expectedScope: contract.scope_name },
          });
          return;
        }
      }

      // Any other literal that does not match the api://<client-id>/<scope> pattern
      if (!raw.startsWith("api://")) {
        context.report({
          node,
          messageId: "forbidden",
          data: { value: raw, expectedScope: contract.scope_name },
        });
        return;
      }

      // Literal IS an api:// scope but doesn't match the contract
      context.report({
        node,
        messageId: "wrong_scope_name",
        data: {
          actual: raw,
          expected: `api://${contract.client_id}/${contract.scope_name}`,
        },
      });
    }

    /**
     * Stringify an AST expression node for comparison against the
     * allowed env-var passthrough list. Returns null if the expression
     * isn't a recognised shape.
     *
     * Supports: process.env.NAME and process.env.NAME || "fallback"
     * (the actual shape used in msal-config.ts:47).
     */
    function expressionAsEnvRef(expr) {
      if (!expr) return null;
      // process.env.NAME
      if (
        expr.type === "MemberExpression" &&
        expr.object?.type === "MemberExpression" &&
        expr.object.object?.name === "process" &&
        expr.object.property?.name === "env" &&
        expr.property?.name
      ) {
        return `process.env.${expr.property.name}`;
      }
      // process.env.NAME || "fallback"
      if (expr.type === "LogicalExpression" && expr.operator === "||") {
        return expressionAsEnvRef(expr.left);
      }
      return null;
    }

    function checkTemplateLiteral(node) {
      // Accept only the canonical shape:
      //   `api://${<env-ref-resolving-to-client-id>}/<literal-scope-name>`
      // where the literal scope name matches the contract AND the
      // interpolation is a recognised client-id env var.
      if (node.expressions.length === 0) {
        // No interpolation → treat as literal
        checkLiteral(node, node.quasis[0]?.value.raw ?? "");
        return;
      }
      // Skeleton check: must be exactly "api://" + ONE EXPR + "/<scope>"
      const parts = node.quasis.map((q) => q.value.raw);
      if (
        node.expressions.length !== 1 ||
        parts.length !== 2 ||
        parts[0] !== "api://" ||
        !parts[1].startsWith("/")
      ) {
        context.report({
          node,
          messageId: "unresolvable_template",
          data: {
            raw: parts.join("${...}"),
            expectedScope: contract.scope_name,
          },
        });
        return;
      }
      // Codex P2 plan-review iter 1: verify the interpolation IS one of
      // the canonical client-id env-var refs. Catches a future drift where
      // someone interpolates a different identifier.
      const envRef = expressionAsEnvRef(node.expressions[0]);
      if (!envRef || !allowedClientIdEnvRefs.has(envRef)) {
        context.report({
          node,
          messageId: "unresolvable_template",
          data: {
            raw:
              `api://\${${envRef ?? "<unrecognised expression>"}}` + parts[1],
            expectedScope: contract.scope_name,
          },
        });
        return;
      }
      const literalScopeName = parts[1].slice(1); // strip leading "/"
      if (literalScopeName !== contract.scope_name) {
        context.report({
          node,
          messageId: "wrong_scope_name",
          data: { actual: literalScopeName, expected: contract.scope_name },
        });
      }
    }

    function visitScopesArray(node) {
      for (const el of node.elements) {
        if (el === null) continue;
        if (el.type === "Literal" && typeof el.value === "string") {
          checkLiteral(el, el.value);
        } else if (el.type === "TemplateLiteral") {
          checkTemplateLiteral(el);
        }
      }
    }

    return {
      // `scopes: [...]` in any object literal
      'Property[key.name="scopes"] > ArrayExpression'(node) {
        visitScopesArray(node);
      },
      // Defensive: catch top-level `export const xxxRequest = { scopes: [...] }`
      // via the same Property selector above.
    };
  },
});
```

- [ ] **Step 6.3 — Write `frontend/eslint-rules/index.mjs`**

```javascript
import { msalScopesMustBeApiOrOidc } from "./msal-scopes.mjs";

export const rules = {
  "msal-scopes-must-be-api-or-oidc": msalScopesMustBeApiOrOidc,
};

export default { rules };
```

- [ ] **Step 6.4 — Write `frontend/eslint-rules/__tests__/msal-scopes.test.mjs`**

```javascript
import { RuleTester } from "@typescript-eslint/rule-tester";
// Codex P2 plan-review iter 2: node:test does NOT export `afterAll`. Use `after`
// and bind it to RuleTester.afterAll (which the lib expects). Confirmed via
// `node -e "import('node:test').then(m => console.log('afterAll' in m))"` → false.
import { after, describe, it } from "node:test";
import { msalScopesMustBeApiOrOidc } from "../msal-scopes.mjs";

RuleTester.afterAll = after;
RuleTester.it = it;
RuleTester.itOnly = it;
RuleTester.describe = describe;

const ruleTester = new RuleTester();

// NOTE: identity-contract.json on the test runner's cwd provides the contract
// values. The rule reads from `<cwd>/../identity-contract.json` (from frontend/
// upward), so RuleTester runs see the same contract as the production rule.

ruleTester.run("msal-scopes-must-be-api-or-oidc", msalScopesMustBeApiOrOidc, {
  valid: [
    {
      code: `const r = { scopes: ["openid", "profile", "email"] }`,
    },
    {
      code: `const r = { scopes: ["openid", "offline_access"] }`,
    },
    {
      // Canonical SPA shape with template literal (matches current msal-config.ts)
      code: "const r = { scopes: [`api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID}/access_as_user`] }",
    },
    {
      code: `instance.acquireTokenSilent({ scopes: ["openid"] })`,
    },
  ],
  invalid: [
    {
      code: `const r = { scopes: ["User.Read"] }`,
      errors: [{ messageId: "forbidden" }],
    },
    {
      code: `const r = { scopes: ["https://graph.microsoft.com/User.Read"] }`,
      errors: [{ messageId: "forbidden" }],
    },
    {
      code: "const r = { scopes: [`api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID}/.default`] }",
      errors: [{ messageId: "wrong_scope_name" }],
    },
    {
      code: `instance.loginRedirect({ scopes: ["User.Read", "openid"] })`,
      errors: [{ messageId: "forbidden" }],
    },
    {
      // Frontend env-var prefix gate — process.env.AZURE_CLIENT_ID is a
      // BACKEND env var that Next.js won't expose to the browser. The
      // canonical frontend form is NEXT_PUBLIC_AZURE_CLIENT_ID. The rule
      // rejects the non-prefixed form as an unresolvable template (would
      // resolve to undefined at browser runtime). Codex P2 plan-review iter 2.
      code: "const r = { scopes: [`api://${process.env.AZURE_CLIENT_ID}/access_as_user`] }",
      errors: [{ messageId: "unresolvable_template" }],
    },
    {
      // Mixed-resource — Graph scope alongside OIDC scopes (avoids hardcoding
      // a specific client GUID in the fixture so the test is stable across
      // Task 10's contract value swap. Codex P2 plan-review iter 2.)
      code: `const r = { scopes: ["openid", "User.Read"] }`,
      errors: [{ messageId: "forbidden" }],
    },
  ],
});
```

- [ ] **Step 6.5 — Run the RuleTester**

```bash
cd frontend && pnpm exec node --test eslint-rules/__tests__/msal-scopes.test.mjs
```

Expected: all valid + invalid cases pass.

If the test runner config differs (e.g., vitest is used), adjust the test runner invocation — but keep the test fixtures intact.

- [ ] **Step 6.6 — Commit Task 6 (rule code, not yet wired)**

```bash
git add frontend/eslint-rules frontend/package.json frontend/pnpm-lock.yaml
git commit -m "feat(auth-gate): add Gate 1 ESLint custom rule msal-scopes-must-be-api-or-oidc"
```

---

## Task 7 — Wire Gate 1 into `frontend/eslint.config.mjs`

**Goal:** ESLint 9 flat config registers the `msai` plugin and turns the rule ON.

**Files:**

- Modify: `frontend/eslint.config.mjs`

- [ ] **Step 7.1 — Read current `eslint.config.mjs`**

```bash
cat frontend/eslint.config.mjs
```

Note the existing exports (likely `next/core-web-vitals` + project tsconfig). The flat config is an array; we append our plugin.

- [ ] **Step 7.2 — Append the `msai` plugin block**

Add to `eslint.config.mjs` (top of file imports, end of array for the rule):

```javascript
// Top of file imports
import msai from "./eslint-rules/index.mjs";

// ... existing config blocks ...

// Append at the END of the exported array:
{
  files: ["src/**/*.ts", "src/**/*.tsx"],
  plugins: { msai },
  rules: {
    "msai/msal-scopes-must-be-api-or-oidc": "error",
  },
},
```

If the project's eslint.config currently imports configs synchronously, leave that pattern; just add the `msai` block.

- [ ] **Step 7.3 — Run `pnpm lint` and verify the rule fires on the CURRENT codebase (should be clean)**

```bash
cd frontend && pnpm lint
```

Expected: PASS. The current `msal-config.ts:42-49` matches the canonical shape, so no `msai/...` errors.

If lint fails:

- Check the rule loaded the contract — `console.log(contract)` in the rule's `loadContract` can be added temporarily to verify
- Check the imported plugin shape — ESLint 9 requires `plugins: { msai: { rules: {...} } }`; we re-export via `index.mjs` to match.

- [ ] **Step 7.4 — Test that lint FAILS when we inject a Graph scope (temporary local change, do NOT commit)**

In `frontend/src/lib/msal-config.ts:43-48`, replace `"openid"` with `"User.Read"`. Save. Run:

```bash
cd frontend && pnpm lint
```

Expected: FAIL with message like:

```
frontend/src/lib/msal-config.ts:44:5: error  Forbidden MSAL scope literal "User.Read" — expected ... (msai/msal-scopes-must-be-api-or-oidc)
```

Revert the change:

```bash
cd frontend && git checkout src/lib/msal-config.ts
cd frontend && pnpm lint  # back to PASS
```

- [ ] **Step 7.5 — Commit Task 7**

```bash
git add frontend/eslint.config.mjs
git commit -m "build(eslint): register msai plugin + enable msal-scopes-must-be-api-or-oidc rule"
```

---

## Task 8 — CI workflow `.github/workflows/auth-gate.yml`

**Goal:** Two parallel jobs on every PR to `main`: `frontend-lint` (pnpm lint) + `backend-auth-gate` (uv pytest of both integration test files).

**Files:**

- Create: `.github/workflows/auth-gate.yml`

- [ ] **Step 8.1 — Write the workflow file**

```yaml
name: Auth Regression Gate

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

concurrency:
  group: auth-gate-${{ github.ref }}
  cancel-in-progress: true

jobs:
  frontend-lint:
    name: Gate 1 — MSAL scope lint
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: pnpm/action-setup@v4
        with:
          version: 9

      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: pnpm
          cache-dependency-path: frontend/pnpm-lock.yaml

      - name: Install frontend dependencies
        working-directory: frontend
        run: pnpm install --frozen-lockfile

      - name: ESLint (Gate 1 — MSAL scope rule)
        working-directory: frontend
        run: pnpm lint

  backend-auth-gate:
    name: Gates 2+3 — JWT contract + identity-contract lint
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install backend dependencies
        working-directory: backend
        run: uv sync --extra dev

      - name: Pytest (Gate 2 — JWT contract)
        working-directory: backend
        run: uv run pytest tests/integration/auth_gate/test_auth_contract.py -v

      - name: Pytest (Gate 3 — identity-contract lint)
        working-directory: backend
        run: uv run pytest tests/integration/auth_gate/test_identity_contract.py -v
```

- [ ] **Step 8.2 — Validate the YAML locally**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/auth-gate.yml')); print('OK')"
```

Expected: `OK`.

- [ ] **Step 8.3 — Commit Task 8**

```bash
git add .github/workflows/auth-gate.yml
git commit -m "ci(auth-gate): add GH Actions workflow — Gate 1 + Gates 2/3"
```

---

## Task 9 — WebSocket JWT path contract test (Contrarian carry-over)

**Goal:** The Contrarian flagged `validate_token_or_api_key` (auth.py:78-89) as a second call site. Codex P2 from plan-review iter 1 sharpened this: existing WS tests **monkeypatch** `validate_token_or_api_key` (see `backend/tests/unit/test_websocket_live_stream.py`) so they never actually exercise the real validator. We add a direct test for the helper to lock in the contract.

**Files:**

- Modify: `backend/tests/integration/auth_gate/test_auth_contract.py` (append direct tests)

- [ ] **Step 9.1 — Confirm the helper structure**

```bash
grep -A 15 "def validate_token_or_api_key" backend/src/msai/core/auth.py
```

The helper calls `self._validator.validate_token(token)` for the JWT path — same code path as `get_current_user`. So Task 2's extension applies uniformly. The test below verifies the helper directly, no WS plumbing needed.

- [ ] **Step 9.2 — Append direct tests to `backend/tests/integration/auth_gate/test_auth_contract.py`**

```python
# ---------------------------------------------------------------------------
# Task 9 — direct test for validate_token_or_api_key (WS path helper).
# Codex P2 plan-review iter 1: existing WS tests monkeypatch this helper
# rather than exercise it, so a JWT-shape regression here can ship undetected.
# ---------------------------------------------------------------------------

def test_validate_token_or_api_key_accepts_valid_jwt(
    validator: EntraIDValidator,
    make_token: Callable[..., str],
    identity_contract: dict[str, Any],
) -> None:
    """The WS helper path must accept a canonical delegated token."""
    from msai.core.auth import validate_token_or_api_key

    token = make_token()
    payload = validate_token_or_api_key(token)
    assert payload["scp"] == identity_contract["scope_name"]
    assert payload["ver"] == identity_contract["token_version"]


def test_validate_token_or_api_key_rejects_missing_scp(
    validator: EntraIDValidator,
    make_token: Callable[..., str],
) -> None:
    """The WS helper path must reject a JWT missing scp (Task 2 extension)."""
    from msai.core.auth import validate_token_or_api_key

    token = make_token(scp=None)
    with pytest.raises(jwt.MissingRequiredClaimError):
        validate_token_or_api_key(token)


def test_validate_token_or_api_key_accepts_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API-key path is independent of the JWT path — must continue to work."""
    from msai.core import auth as auth_module
    from msai.core.auth import validate_token_or_api_key

    monkeypatch.setattr(auth_module.settings, "msai_api_key", "test-api-key-value")
    payload = validate_token_or_api_key("test-api-key-value")
    assert payload["sub"] == "api-key-user"
```

- [ ] **Step 9.3 — Run the new tests**

```bash
cd backend && uv run pytest tests/integration/auth_gate/test_auth_contract.py -v -k "validate_token_or_api_key"
```

Expected: 3 PASSED.

- [ ] **Step 9.4 — Run the full backend test suite to surface any cross-cutting failure**

```bash
cd backend && uv run pytest tests/ -v
```

Expected: all PASS. If any test fails because it minted a token without `scp`/`ver`, update the mint to use the canonical defaults from `tests/unit/test_auth.py::_make_token` (or `tests/integration/auth_gate/conftest.py::make_token`).

- [ ] **Step 9.5 — Commit Task 9**

```bash
git add backend/tests/integration/auth_gate/test_auth_contract.py
git commit -m "test(auth-gate): direct contract test for validate_token_or_api_key (WS path)"
```

---

## Task 10 — Populate `identity-contract.json` with real production values

**Goal:** Replace the all-zero placeholder GUIDs with the real MSAI production tenant/client IDs, so Gate 3's drift detection has a real baseline. This is the ONE manual step Pablo performs locally before the negative-control demo.

**Files:**

- Modify: `identity-contract.json`

- [ ] **Step 10.1 — Read the real GUIDs from local `.env`**

The values are public OAuth identifiers, not secrets, but for safety Pablo copies them directly:

```bash
grep -E "^AZURE_TENANT_ID|^AZURE_CLIENT_ID" .env
```

- [ ] **Step 10.2 — Replace the placeholder GUIDs in `identity-contract.json`**

Edit `identity-contract.json` and replace every `00000000-0000-0000-0000-000000000000` placeholder with the corresponding real GUID:

- `tenant_id` ← `AZURE_TENANT_ID`
- `client_id` ← `AZURE_CLIENT_ID`
- `app_id_uri` ← `api://<AZURE_CLIENT_ID>`
- `issuer` ← `https://login.microsoftonline.com/<AZURE_TENANT_ID>/v2.0`

- [ ] **Step 10.3 — Re-run Gate 3 locally**

```bash
cd backend && uv run pytest tests/integration/auth_gate/test_identity_contract.py -v
```

Expected: all 4 tests PASS. If the real GUIDs differ from values currently in some `.env.example` file, Gate 3 fails — fix the drifted file.

- [ ] **Step 10.4 — Commit**

```bash
git add identity-contract.json
git commit -m "feat(auth-gate): populate identity-contract.json with production GUIDs"
```

---

## Task 11 — Negative-control commit pair (PRD US-004)

**Goal:** Durable PR-level proof that Gate 1 fires on real `User.Read` regression. Two commits on this branch: the demo regression + the revert. Reviewers see the failing CI run in the PR history.

**Files:**

- Temporarily modify: `frontend/src/lib/msal-config.ts` (revert in next commit)

- [ ] **Step 11.1 — Confirm the local lint is currently clean**

```bash
cd frontend && pnpm lint
```

Expected: PASS.

- [ ] **Step 11.2 — Inject the May 20 regression deliberately**

Edit `frontend/src/lib/msal-config.ts:42-49`. Replace the array contents so the FIRST scope is `"User.Read"`:

```typescript
export const loginRequest = {
  scopes: [
    "User.Read", // DEMO REGRESSION — Gate 1 must catch this
    "openid",
    "profile",
    "email",
    `api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID || ""}/access_as_user`,
  ],
};
```

- [ ] **Step 11.3 — Run lint locally; expect FAIL with the structured message**

```bash
cd frontend && pnpm lint
```

Expected output includes:

```
frontend/src/lib/msal-config.ts:44:5: error  Forbidden MSAL scope literal "User.Read" — expected "api://<client-id>/access_as_user" or one of openid/profile/email/offline_access. (msai/msal-scopes-must-be-api-or-oidc)
```

- [ ] **Step 11.4 — Commit the DEMO regression (CI will fail — that's the point)**

```bash
git add frontend/src/lib/msal-config.ts
git commit -m "demo: revert MSAL scope to User.Read — auth-gate must catch this"
```

Push, then wait for CI:

```bash
git push -u origin feat/ui-e2e-auth-setup
gh run watch
```

CI run for THIS commit will fail with the structured Gate 1 diagnostic. Record the URL of the failing run — it goes into the PR body.

- [ ] **Step 11.5 — Revert the demo regression**

Edit `frontend/src/lib/msal-config.ts` back to the original:

```typescript
export const loginRequest = {
  scopes: [
    "openid",
    "profile",
    "email",
    `api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID || ""}/access_as_user`,
  ],
};
```

- [ ] **Step 11.6 — Confirm lint passes again locally**

```bash
cd frontend && pnpm lint
```

Expected: PASS.

- [ ] **Step 11.7 — Commit the revert and push**

```bash
git add frontend/src/lib/msal-config.ts
git commit -m "revert: restore correct MSAL scope (auth-gate demo passed above)"
git push
```

CI on the revert MUST pass. Capture this URL too — the PR body will reference both: the RED run from Step 11.4 and the GREEN run after the revert.

---

## Self-Review

**1. Spec coverage check:**

| Spec section                                            | Task(s) implementing | Notes                                                       |
| ------------------------------------------------------- | -------------------- | ----------------------------------------------------------- |
| Gate 1 (custom ESLint rule, `@typescript-eslint/utils`) | Tasks 6, 7           | RuleTester fixtures + flat-config registration              |
| Gate 2 (synthetic JWT contract, ASGITransport)          | Tasks 3, 4           | conftest scaffolding + 11 parametrized cases + smoke test   |
| Backend `validate_token` extension (`scp` + `ver`)      | Task 2               | TDD: failing tests first, then extension, then ripple fixes |
| Gate 3 (identity-contract.json file-walker)             | Tasks 1, 5           | Schema + contract + lint with 4 test cases                  |
| `identity-contract.json` foundation                     | Tasks 1, 10          | Placeholder shipped Task 1; real values populated Task 10   |
| CI workflow (2 parallel jobs)                           | Task 8               | `frontend-lint` + `backend-auth-gate`                       |
| WebSocket path verification (Contrarian carry-over)     | Task 9               | Read-only verification + full backend test sweep            |
| Negative-control commit pair (US-004)                   | Task 11              | Demo regression → revert; PR body links the failing CI URL  |

All 4 PRD user stories (US-001 through US-004) map to tasks. No gaps.

**2. Placeholder scan:** No TBDs, no "implement later," no "similar to Task N" without code. The contract file ships with `00000000-...` placeholders by deliberate design (Task 1 documents this; Task 10 replaces them) — that's a feature, not a placeholder bug.

**3. Type consistency check:**

- `_EXPECTED_SCOPE` and `_EXPECTED_TOKEN_VERSION` (Task 2) referenced consistently
- `make_token` factory signature (Task 3) matches the override keys used in `AUTH_CASES` (Task 4)
- `_MockJWKSClient.get_signing_key_from_jwt` matches `PyJWKClient`'s real signature (Task 3) — verified against PyJWT 2.13.0 source per research §3
- `msalScopesMustBeApiOrOidc` rule export name consistent across `msal-scopes.ts`, `index.ts`, `eslint.config.mjs`, and the RuleTester run name (Tasks 6, 7)
- ESLint rule message IDs (`forbidden`, `wrong_scope_name`, `unresolvable_template`) declared in `meta.messages` and referenced consistently in `context.report` calls

**4. Acknowledged inherent risk:** The Contrarian flagged that `validate_token` is also called by the WebSocket JWT path (`validate_token_or_api_key`). Task 9 verifies the WS path. The wider risk (live tokens missing `scp`) is mitigated by: (a) all real Entra v2.0 delegated tokens carry `scp` per Microsoft docs; (b) the autouse override in `tests/conftest.py` covers the unit test paths; (c) the post-deploy Bearer probe (deferred follow-up PR) will catch any environment-specific drift.

---

**End of plan. 11 tasks. Plan-review loop PASSED iter 5 (Codex clean).**

---

## Dispatch Plan

**Sequential mode.** This plan is tightly coupled: Task 2 extends `validate_token` which Task 3's conftest validator-fixture relies on; Task 4's test cases depend on Task 3's conftest fixtures; Task 5 + Task 6 both depend on Task 1's `identity-contract.json`; Task 7 wires Task 6's rule; Task 8 invokes all gates in CI; Task 9 modifies Task 4's test file (same path — disjointness violation); Task 10 modifies Task 1's contract file (same path); Task 11's negative-control needs the full stack live. Per workflow Phase 4.0 "Sequential override: if the plan is tightly coupled, dispatch one subagent at a time" — that's the right mode here.

| Task ID | Depends on  | Writes (concrete file paths)                                                                                                                                                           |
| ------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1       | —           | `identity-contract.json`, `identity-contract.schema.json`, `identity-contract.README.md`                                                                                               |
| 2       | —           | `backend/src/msai/core/auth.py`, `backend/tests/unit/test_auth.py`                                                                                                                     |
| 3       | 1, 2        | `backend/tests/integration/auth_gate/__init__.py`, `backend/tests/integration/auth_gate/conftest.py`                                                                                   |
| 4       | 3           | `backend/tests/integration/auth_gate/test_auth_contract.py`                                                                                                                            |
| 5       | 1           | `backend/tests/integration/auth_gate/test_identity_contract.py`, `backend/pyproject.toml`                                                                                              |
| 6       | 1           | `frontend/eslint-rules/msal-scopes.mjs`, `frontend/eslint-rules/index.mjs`, `frontend/eslint-rules/__tests__/msal-scopes.test.mjs`, `frontend/package.json`, `frontend/pnpm-lock.yaml` |
| 7       | 6           | `frontend/eslint.config.mjs`                                                                                                                                                           |
| 8       | 4, 5, 7     | `.github/workflows/auth-gate.yml`                                                                                                                                                      |
| 9       | 4           | `backend/tests/integration/auth_gate/test_auth_contract.py` (modify — same-file conflict with Task 4, MUST serialize after 4)                                                          |
| 10      | 1, 8        | `identity-contract.json` (modify — same-file conflict with Task 1, MUST serialize after 1)                                                                                             |
| 11      | 6, 7, 8, 10 | `frontend/src/lib/msal-config.ts` (modify twice — demo regression commit, then revert)                                                                                                 |

**Dispatch order (linear):** 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11. No parallelism — same-file conflicts and dependency depth keep the path serial.

---

#### E2E Use Cases

**E2E: N/A — internal tooling + behavior-preserving backend hardening.**

**Surface coverage decision** (per `rules/testing.md` requirements; project exposes UI, API, CLI per `CLAUDE.md ## E2E Configuration`):

- **UI:** N/A — feature adds no UI, no user-facing pages, no flow change. The existing MSAL → PyJWT flow continues to issue tokens with `scp=access_as_user` (verified by inspecting Entra-issued tokens in production per PR #75); valid users see no behavior difference. Live-Entra UI verification of this auth path is deferred to the **post-deploy Bearer probe** PR queued in `state.md ## Next` — that's the appropriate test surface for live integration drift, not PR-time E2E.
- **API:** N/A — no new endpoint, no contract change for valid tokens. The backend now correctly rejects previously-incorrectly-accepted invalid tokens (missing `scp`, wrong `scp`, wrong `ver`, v1.0 issuer). For valid Entra-issued v2.0 delegated tokens, behavior is unchanged. Gate 2 (Task 4) IS the contract test for the auth middleware — it exercises 11 cases through the real `/api/v1/account/health` handler via ASGITransport. That covers the API surface contract test-tier, which is the appropriate tier given no user-facing change exists at the API.
- **CLI:** N/A — feature does not touch the `X-API-Key` non-human auth path. CLI behavior unchanged. Gate 2's smoke test asserts the API-key path remains independent.

**Why this satisfies the rules/testing.md N/A guidance:**

This is a behavior-preserving security hardening that closes a regression coverage gap. The PRD's primary deliverable is **CI tooling** (the three gates themselves), which is explicitly listed in `rules/testing.md` as a category that may be E2E: N/A ("CI config, dev tooling, behavior-preserving refactors"). The `validate_token` extension is in scope per NO BUGS LEFT BEHIND but does not change observable behavior for any valid user flow — the extension only rejects tokens that should never have been accepted in the first place.

The negative-control commit (Task 11) is the durable proof artifact that the gate fires. It IS the user-journey-equivalent for this feature: a real PR-level demonstration that a malformed config causes a real CI failure with a real diagnostic.

**Future live-Entra E2E:**

The deferred **post-deploy Bearer probe** PR (queued in `state.md ## Next`, ~30 min) will add a probe that hits the deployed `/api/v1/account/health` with a real Bearer token captured from a real MSAL flow. That probe IS the live-integration test surface; it lives in `deploy.yml` (post-deploy), not on PR. This PR does not duplicate that work.
