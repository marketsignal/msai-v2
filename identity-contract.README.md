# Identity Contract

`identity-contract.json` is the **single source of truth** for MSAI v2's Entra ID identity wiring.

## What it contains

Public OAuth identifiers: tenant ID, client ID, app ID URI, issuer URL, scope name, token version, and the canonical env-var names that reference them.

## What it does NOT contain

Secrets. The contract is committed to git. Real secrets (passwords, signing keys, API keys, refresh tokens) live in `.env` (gitignored) or GitHub Actions secrets.

> **Why tenant/client IDs are not secrets:** they're public OAuth identifiers that ship in every browser's MSAL JS bundle and in the issuer URL of every Entra-issued JWT. Knowing them does not grant any access.

## Who reads it

- **Gate 1** (`frontend/eslint-rules/msal-scopes.mjs`) — validates MSAL scope literals against `app_id_uri + scope_name`.
- **Gate 2** (`backend/tests/integration/auth_gate/test_auth_contract.py`) — mints positive-case tokens with `aud=client_id`, `iss=issuer`, `scp=scope_name`, `ver=token_version`.
- **Gate 3** (`backend/tests/integration/auth_gate/test_identity_contract.py`) — file-walker that asserts every `AZURE_TENANT_ID`/`AZURE_CLIENT_ID` reference in `.env.example`, compose, and workflow files matches the contract.

## Runtime consumption

**None.** Runtime config flows through env vars (pydantic-settings on the backend, `NEXT_PUBLIC_*` on the frontend). The contract is enforced only at lint time and test time.

## Updating the contract

If you need to rotate the tenant or client ID (e.g., new app registration, separate test tenant for the multi-account-fleet initiative):

1. Update `identity-contract.json` with the new values.
2. Update `.env` / GH Actions secrets / Azure config to match.
3. Run `cd backend && uv run pytest tests/integration/auth_gate/test_identity_contract.py -v` locally — should pass once everything is consistent.
4. Open PR. CI Gate 3 enforces the contract automatically.

## Future: multi-tenant arrays

If MSAI ever needs to operate against multiple Entra tenants concurrently (e.g., per-customer tenants in the broker-fleet initiative), the schema will grow to accept arrays of contracts. The current v1 shape covers single-tenant deployment.
