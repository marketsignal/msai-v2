# Design: Auth Regression Gate — Static Lint + Synthetic JWT Contract

**Date:** 2026-05-25
**Spec for:** PRD `docs/prds/ui-e2e-auth-setup.md` v2.0
**Research brief:** `docs/research/2026-05-25-ui-e2e-auth-setup.md`
**Author:** Claude (autonomous-loop mode)
**Status:** Ready for `/council` contrarian gate + `writing-plans`

---

## 1. Architecture Overview

Three deterministic PR-time gates run as **two GitHub Actions jobs** (one frontend, one backend) and gate merge to `main`. All gates execute with **no network access** — synthetic tokens, mocked JWKS, file-system reads only. Total wall-clock target ≤ 5 s per gate; ≤ 2 min total including stack startup.

```
┌──────────────────────────────────────────────────────────────────────┐
│                      identity-contract.json                          │
│   (committed at repo root, non-secret single source of truth)        │
│   tenant_id / client_id / app_id_uri / issuer / scope_name / ver     │
└─────────────────────┬────────────────────────────────────────────────┘
                      │ read by all 3 gates
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│  Gate 1      │ │  Gate 2      │ │  Gate 3      │
│  MSAL scope  │ │  JWT middle- │ │  Cross-file  │
│  AST lint    │ │  ware contr. │ │  consistency │
│  (ESLint     │ │  (pytest +   │ │  (pytest)    │
│   custom     │ │  synth JWT)  │ │              │
│   rule)      │ │              │ │              │
└──────┬───────┘ └──────┬───────┘ └──────┬───────┘
       │                │                │
       │     ┌──────────┴──────┐         │
       ▼     ▼                 ▼         ▼
┌──────────────────┐  ┌─────────────────────────┐
│ frontend-lint    │  │   backend-auth-gate     │  (CI jobs)
│ (pnpm lint)      │  │   (uv run pytest        │
│                  │  │    tests/integration/   │
│                  │  │    test_auth_*.py)      │
└──────────────────┘  └─────────────────────────┘
```

**Key decisions captured here:**

- Gate 1 → custom ESLint rule (research §1, Option A)
- Gate 2 → pytest with patched JWKS + synthetic JWT (research §3-4)
- Gate 3 → pytest with file-walker (Python chosen — shares fixtures with Gate 2, single backend job)
- Identity contract committed at repo root; non-secret; lint/test-only consumption
- Backend `validate_token` MUST be extended (research §3 Open Risk #1 — NO BUGS LEFT BEHIND argues yes; Codex endorsed)

## 2. File Inventory (concrete deliverables)

| File                                                  | Action      | Purpose                                                                                               |
| ----------------------------------------------------- | ----------- | ----------------------------------------------------------------------------------------------------- |
| `identity-contract.json`                              | NEW (root)  | Single source of truth for tenant_id / client_id / app_id_uri / issuer / scope_name / token_version   |
| `identity-contract.README.md`                         | NEW (root)  | Rationale, schema, "do not duplicate values" rule, future-multi-account note                          |
| `identity-contract.schema.json`                       | NEW (root)  | JSON Schema for `identity-contract.json` — enables IDE auto-complete + Gate 3 shape validation        |
| `frontend/eslint-rules/msal-scopes.ts`                | NEW         | Custom ESLint rule (Gate 1) — AST inspection of MSAL scope arrays                                     |
| `frontend/eslint-rules/__tests__/msal-scopes.test.ts` | NEW         | RuleTester fixtures for Gate 1 (positive + negative cases)                                            |
| `frontend/eslint.config.mjs`                          | EDIT        | Register the new rule under a `msai/` plugin namespace (ESLint 9 flat config)                         |
| `frontend/package.json`                               | EDIT        | Add `@typescript-eslint/utils` + `@typescript-eslint/rule-tester` devDeps                             |
| `backend/src/msai/core/auth.py`                       | EDIT        | Extend `validate_token`: enforce `scp` claim presence + content (`access_as_user`); enforce `ver=2.0` |
| `backend/tests/integration/test_auth_contract.py`     | NEW         | Gate 2 — 9+ negative cases + 1 positive + smoke test (autouse override cleared)                       |
| `backend/tests/integration/test_identity_contract.py` | NEW         | Gate 3 — schema validate + cross-file consistency check                                               |
| `backend/tests/integration/conftest.py`               | EDIT or NEW | Module-scoped fixture: clear `app.dependency_overrides[get_current_user]`; network-block autouse      |
| `.github/workflows/auth-gate.yml`                     | NEW         | 2 jobs: `frontend-lint` (pnpm lint) + `backend-auth-gate` (uv pytest)                                 |
| `docs/runbooks/auth-gate.md` (optional)               | NEW         | When the gate fires: how to diagnose by layer                                                         |

**Note on test file naming:** Both Gate 2 and Gate 3 are `pytest` tests under `tests/integration/`. They share the conftest's network-block autouse fixture and identity-contract loading. They run in the same `backend-auth-gate` job (pytest `-n auto` for parallelism). This keeps the CI shape simple while keeping the test files single-purpose.

## 3. `identity-contract.json` — schema and location

**Location:** Repo root. Rationale: maximum visibility; future contributors discover it immediately. Sibling `identity-contract.README.md` explains the convention since there's no industry precedent.

**Schema** (informed by research §6):

```json
{
  "$schema": "./identity-contract.schema.json",
  "tenant_id": "24a60bec-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
  "client_id": "24a60bec-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
  "app_id_uri": "api://24a60bec-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
  "issuer": "https://login.microsoftonline.com/24a60bec-XXXX-XXXX-XXXX-XXXXXXXXXXXX/v2.0",
  "scope_name": "access_as_user",
  "token_version": "2.0",
  "frontend_env_prefix": "NEXT_PUBLIC_",
  "env_var_names": {
    "tenant_id": ["AZURE_TENANT_ID", "JWT_TENANT_ID"],
    "client_id": ["AZURE_CLIENT_ID", "JWT_CLIENT_ID"]
  }
}
```

**Non-secret declaration:** Tenant ID and client ID are PUBLIC identifiers in OAuth2 — they are not secrets and are safe to commit. (They appear in every browser-visible MSAL config already.) Real secrets (passwords, signing keys, API keys) NEVER go in this file.

**Runtime consumption:** **NONE.** Backend continues to read env vars via pydantic-settings; frontend continues to read `NEXT_PUBLIC_*` at build time. The contract is only consumed by:

1. Gate 3's `test_identity_contract.py` (validates other files match)
2. Gate 1's ESLint rule (resolves `<client-id>` in template-literal scope expressions)
3. Gate 2's `test_auth_contract.py` (mints positive-case tokens using the issuer/audience/scope from this file)

**Note on duplicate placeholder values:** `tenant_id` and `client_id` happen to be the same GUID in the example above ONLY because MSAI's app reg is shared by the SPA and the API (single-app design noted in `msal-config.ts:20-22`). The schema treats them as independent fields so a future multi-app split doesn't break.

## 4. Gate 1 — MSAL scope AST lint (custom ESLint rule)

**Library:** `@typescript-eslint/utils` (canonical 2026 pattern per research §1). Rule registered under `msai/` plugin namespace in ESLint 9 flat config.

**Rule name:** `msai/msal-scopes-must-be-api-or-oidc`

**File layout:**

```
frontend/eslint-rules/
  msal-scopes.ts                       # The rule
  __tests__/msal-scopes.test.ts        # RuleTester fixtures
```

**Detection strategy — import-graph driven, not path-driven** (research §2 design impact):

1. The rule runs on every `.ts`/`.tsx` file in `frontend/src/`.
2. The rule inspects `Property[key.name="scopes"] > ArrayExpression` AND specific MSAL call sites: `CallExpression[callee.property.name=/^(loginRedirect|loginPopup|acquireTokenSilent|acquireTokenPopup|acquireTokenRedirect|ssoSilent)$/]`.
3. For each `scopes: [...]` array literal, every element is classified:
   - **Allowed OIDC** — literal `openid`, `profile`, `email`, `offline_access` → OK
   - **Allowed resource scope** — literal matching `^api://<expected-client-id>/<scope_name>$` where `<expected-client-id>` and `<scope_name>` come from `identity-contract.json` → OK
   - **Known-safe template literal** — pattern `` `api://${EXPR}/<literal-scope-name>` `` where `EXPR` is `process.env.NEXT_PUBLIC_AZURE_CLIENT_ID || ""` or `${IDENTIFIER}` mapped to the known env-var, AND the literal scope name matches `identity-contract.json#scope_name` → OK
   - **Forbidden** — contains `User.Read`, `graph.microsoft.com`, `.default`, OR any other literal/template not matching the above → REPORT
4. **Spread expressions** (`{ ...loginRequest, account }`) — the rule follows the spread to its source object (within the same file's `export` statements). If unresolvable across files, the rule **reports a separate diagnostic** that requires the source to be in the file under review (cheap fix: re-export the scope array locally). This avoids the cross-file resolution complexity.

**Output format:**

```
frontend/src/lib/msal-config.ts:47: error  Forbidden MSAL scope literal "User.Read" — expected api://<client-id>/access_as_user (msai/msal-scopes-must-be-api-or-oidc)
```

**Rule's own test fixtures** (research §1 test implication):

```typescript
ruleTester.run("msai/msal-scopes-must-be-api-or-oidc", rule, {
  valid: [
    {
      code: 'const r = { scopes: ["openid", "profile", "email", "api://abc-123/access_as_user"] }',
    },
    {
      code: "const r = { scopes: [`api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID}/access_as_user`] }",
    },
    { code: 'instance.acquireTokenSilent({ scopes: ["openid"] })' },
  ],
  invalid: [
    {
      code: 'const r = { scopes: ["User.Read"] }',
      errors: [{ messageId: "forbidden" }],
    },
    {
      code: 'const r = { scopes: ["https://graph.microsoft.com/User.Read"] }',
      errors: [{ messageId: "forbidden" }],
    },
    {
      code: 'const r = { scopes: ["api://abc-123/.default"] }',
      errors: [{ messageId: "forbidden" }],
    },
    {
      code: 'instance.loginRedirect({ scopes: ["User.Read"] })',
      errors: [{ messageId: "forbidden" }],
    },
    // mixed-resource (one literal is API, one is Graph)
    {
      code: 'const r = { scopes: ["api://abc/scope", "User.Read"] }',
      errors: [{ messageId: "forbidden" }],
    },
    // template literal with wrong scope name
    {
      code: "const r = { scopes: [`api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID}/WRONG_NAME`] }",
      errors: [{ messageId: "wrong_scope_name" }],
    },
  ],
});
```

**`.default` is FAIL** — PRD §7 open question resolved here. PR #74's outage involved `.default`; we treat it as forbidden in this rule. Future need for `.default` (e.g., a daemon flow) would require explicit allow-list bypass — not gradual drift.

## 5. Gate 2 — Backend JWT middleware contract test

**File:** `backend/tests/integration/test_auth_contract.py` (NEW).

**Strategy** (research §3, §4):

1. **Network-block autouse fixture** (in `backend/tests/integration/conftest.py`): monkey-patches `urllib.request.urlopen`, `httpx.get`, `httpx.AsyncClient.get`, `httpx.AsyncClient.send` to raise `RuntimeError("Network blocked in test")`. Gate 2 ASSERTS no network call happens.
2. **Module-scoped fixture** that clears the autouse override from `backend/tests/conftest.py:49-58`:
   ```python
   @pytest.fixture(scope="module", autouse=True)
   def _clear_unit_auth_override() -> Iterator[None]:
       app.dependency_overrides.pop(get_current_user, None)
       yield
   ```
3. **Test JWKS setup** (session-scoped fixture):
   - Generate one RSA-2048 keypair (the "current" key, kid=`test-kid-current`).
   - Generate a second RSA-2048 keypair (the "old/rotated-out" key, kid=`test-kid-rotated`).
   - Construct a `MagicMock`-backed `PyJWKClient` substitute that:
     - For known `kid` values, returns a `signing_key.key = <PEM bytes>` object
     - For unknown `kid`, raises `jwt.exceptions.PyJWKClientError("kid not found in JWKS")`
   - Inject into the real `EntraIDValidator` instance: `validator._jwks_client = mocked_jwks_client`
   - Use `init_validator()` with the tenant_id/client_id from `identity-contract.json`.
4. **Token factory** `make_entra_token(**overrides)`:
   - Builds the canonical positive payload (research §5 exact shape).
   - Accepts `aud`, `iss`, `scp`, `roles`, `ver`, `exp`, `kid`, `sub`, `azp`, `azpacr` overrides.
   - Signs with the test private key (or, when explicitly told to sign with the "rotated" key for negative case 7, with the rotated key).
   - Returns the signed JWT string.
5. **Smoke test (FIRST test in module)**:
   ```python
   async def test_unit_autouse_override_is_cleared(client: AsyncClient) -> None:
       """If the autouse override in tests/conftest.py leaks here, every other test silently passes."""
       resp = await client.get("/api/v1/auth/me")  # No Authorization, no X-API-Key
       assert resp.status_code == 401
   ```

**Test cases (parametrized):**

| #   | Case                                         | Token override                                                   | Expected exception in `validate_token`                                                         | Expected HTTP status |
| --- | -------------------------------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | -------------------- |
| 0   | **Positive**                                 | All canonical                                                    | (no exception)                                                                                 | 200                  |
| 1   | Graph audience                               | `aud="00000003-0000-0000-c000-000000000000"`                     | `InvalidAudienceError`                                                                         | 401                  |
| 2   | Wrong tenant in issuer                       | `iss="https://login.microsoftonline.com/wrong-tenant-uuid/v2.0"` | `InvalidIssuerError`                                                                           | 401                  |
| 3   | Missing `scp` claim                          | `scp=DELETE`                                                     | `MissingRequiredClaimError` (after backend extension)                                          | 401                  |
| 4   | Wrong `scp` value                            | `scp="User.Read"`                                                | `InvalidTokenError` with msg `Required scope 'access_as_user' missing from scp claim` (custom) | 401                  |
| 5   | Expired                                      | `exp=<10s in past>`                                              | `ExpiredSignatureError`                                                                        | 401                  |
| 6   | Bad `kid` (JWKS lookup miss)                 | `kid="unknown-kid"`                                              | `PyJWKClientError`                                                                             | 401                  |
| 7   | Wrong signature (signed with rotated key)    | Signed with rotated keypair but `kid="test-kid-current"`         | `InvalidSignatureError`                                                                        | 401                  |
| 8   | App-only `roles` claim without `scp`         | `roles=["api.access"]`, `scp=DELETE`                             | `MissingRequiredClaimError` (after backend extension)                                          | 401                  |
| 9   | v1.0 issuer URL host                         | `iss="https://sts.windows.net/<tenant>/"`                        | `InvalidIssuerError` (different host than v2.0 issuer)                                         | 401                  |
| 10  | `ver` claim is `"1.0"` with v2.0 issuer host | `ver="1.0"`                                                      | `InvalidTokenError` with msg `Token version mismatch: expected 2.0, got 1.0` (custom)          | 401                  |

**Two layers of assertions per case:**

```python
@pytest.mark.parametrize("case", AUTH_CASES, ids=lambda c: c.id)
async def test_auth_contract(client: AsyncClient, validator: EntraIDValidator, case: AuthCase) -> None:
    token = make_entra_token(**case.overrides)

    # Layer A: the validator itself raises the SPECIFIC exception subclass
    if case.expected_exception:
        with pytest.raises(case.expected_exception):
            validator.validate_token(token)

    # Layer B: the FastAPI dependency surfaces it as the right HTTP error
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == case.expected_status
    if case.expected_status == 401:
        assert "Invalid token" in resp.json()["detail"]
```

**Why two layers:** Layer A is the unit-level contract (PyJWT does the right thing). Layer B is the integration contract (FastAPI dependency surfaces it correctly). The May 20 bug could in theory have been in EITHER layer.

## 6. Backend `validate_token` extension

**File:** `backend/src/msai/core/auth.py` (EDIT).

**Current behavior** (line 46-56):

```python
def validate_token(self, token: str) -> dict[str, Any]:
    signing_key = self._jwks_client.get_signing_key_from_jwt(token)
    payload: dict[str, Any] = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=self._audience,
        issuer=self._issuer,
        options={"require": ["exp", "iss", "aud", "sub"]},
    )
    return payload
```

**Extended behavior:**

```python
# Constants (new) — top of file
_EXPECTED_SCOPE = "access_as_user"
_EXPECTED_TOKEN_VERSION = "2.0"

# Inside class — modified validate_token
def validate_token(self, token: str) -> dict[str, Any]:
    signing_key = self._jwks_client.get_signing_key_from_jwt(token)
    payload: dict[str, Any] = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=self._audience,
        issuer=self._issuer,
        options={"require": ["exp", "iss", "aud", "sub", "scp"]},  # Add "scp"
    )

    # Custom check 1: scp claim contains expected scope (research §3 case 4)
    scp_claim = payload.get("scp", "")
    if _EXPECTED_SCOPE not in scp_claim.split():
        raise jwt.InvalidTokenError(
            f"Required scope '{_EXPECTED_SCOPE}' missing from scp claim "
            f"(got: '{scp_claim}')"
        )

    # Custom check 2: ver claim matches expected version (research §3 case 10)
    ver_claim = payload.get("ver")
    if ver_claim != _EXPECTED_TOKEN_VERSION:
        raise jwt.InvalidTokenError(
            f"Token version mismatch: expected '{_EXPECTED_TOKEN_VERSION}', "
            f"got '{ver_claim}'"
        )

    return payload
```

**Why custom `InvalidTokenError` not a new exception class:**

The existing `get_current_user` dependency catches `jwt.InvalidTokenError` (auth.py:124). Raising the parent class keeps the existing FastAPI 401 handling intact. The detail message names the specific failure.

**Backward-compat note:**

- Adding `"scp"` to the `require` list will reject ANY token without `scp`. **This includes app-only tokens (which use `roles`).** Today the backend only validates user tokens (the WebSocket path uses `validate_token_or_api_key` which is separate). If the system ever needs to accept app-only tokens in the future, that's a separate code path (`validate_app_only_token`) — not a regression of this gate.
- API-key path is untouched (it bypasses `validate_token` entirely via `_API_KEY_CLAIMS`).
- The existing single test in `backend/tests/unit/test_auth.py` may break because it doesn't include `scp`/`ver` in its mint helper. That test will be updated as part of this PR (add `scp` and `ver` to its `_make_token` helper). This is a tiny ripple.

**Risk:** A live token from Entra that doesn't carry `scp` would break prod. Empirically — every delegated token from Entra v2.0 with a `scopes=["api://.../access_as_user"]` request HAS the `scp` claim. The contract test verifies this round-trip. No prod risk for delegated tokens.

## 7. Gate 3 — identity contract cross-file consistency lint

**File:** `backend/tests/integration/test_identity_contract.py` (NEW).

**Strategy** (research §6):

1. **Step A — Schema validate** the `identity-contract.json` file against `identity-contract.schema.json`. If the contract is malformed, fail with a specific error before the cross-file check.
2. **Step B — Build expected-value map** from the contract:
   ```python
   expected = {
       "AZURE_TENANT_ID": contract["tenant_id"],
       "JWT_TENANT_ID": contract["tenant_id"],
       "AZURE_CLIENT_ID": contract["client_id"],
       "JWT_CLIENT_ID": contract["client_id"],
       "NEXT_PUBLIC_AZURE_TENANT_ID": contract["tenant_id"],
       "NEXT_PUBLIC_AZURE_CLIENT_ID": contract["client_id"],
   }
   ```
3. **Step C — File walker** searches the repo for occurrences:
   - `**/*.env.example` (frontend, backend, root)
   - `docker-compose.dev.yml`, `docker-compose.prod.yml`
   - `.github/workflows/*.yml` (looking for hardcoded values that should be env-substituted)
   - **Exclusion list:** `node_modules/`, `.venv/`, `.worktrees/`, `data/`, `.git/`, `backend/.venv/`, `frontend/.next/`, the contract file itself.
4. **Step D — Per file**, parse for `KEY=VALUE` pairs OR `${KEY}` references:
   - If `KEY` is in the expected-value map AND the literal `VALUE` differs from `expected[KEY]` (with placeholder/`your-…` exemptions documented in the lint) → REPORT.
   - If `KEY` is in the expected-value map AND the file uses `${KEY}` (env-var passthrough) → OK.

**Special case for `.env.example` files:** These commonly use placeholder values (`your-tenant-id`, `<GUID>`, etc.) for documentation. The lint MUST allow these. Strategy: accept any value matching `^(your-|<|TBD|EXAMPLE).*` as a placeholder. Real values (UUID/GUID format) must match the contract.

**Output format:**

```
docker-compose.prod.yml:23: drift  AZURE_TENANT_ID="old-tenant-id" but identity-contract.json#tenant_id="24a60bec-..."
```

**Test fixtures for Gate 3 (the lint of the lint):**

```python
def test_drift_is_detected(tmp_path: Path) -> None:
    # Arrange: build a fixture repo with one file in sync and one drifted
    fixture_contract = {...}
    sync_file = tmp_path / ".env.example"
    sync_file.write_text("AZURE_TENANT_ID=24a60bec-aaaa-...\n")
    drift_file = tmp_path / "docker-compose.prod.yml"
    drift_file.write_text("environment:\n  AZURE_TENANT_ID=OLD-DIFFERENT-VALUE\n")

    # Act
    findings = lint_identity_contract(contract=fixture_contract, root=tmp_path)

    # Assert: only the drifted file is reported
    assert len(findings) == 1
    assert findings[0].file.name == "docker-compose.prod.yml"
    assert findings[0].key == "AZURE_TENANT_ID"
```

## 8. CI Workflow

**File:** `.github/workflows/auth-gate.yml` (NEW).

```yaml
name: Auth Regression Gate

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

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
      - working-directory: frontend
        run: pnpm install --frozen-lockfile
      - working-directory: frontend
        run: pnpm lint

  backend-auth-gate:
    name: Gate 2+3 — JWT contract + identity-contract lint
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - working-directory: backend
        run: uv sync --frozen
      - working-directory: backend
        run: uv run pytest tests/integration/test_auth_contract.py tests/integration/test_identity_contract.py -v
```

**Both jobs are required** in branch-protection rules (set after merge by Pablo). Cache strategy is standard (pnpm + uv).

## 9. Negative-Control Commit (PRD US-004)

The PR will include this commit sequence on the feature branch:

1. `feat(auth-gate): add MSAL scope lint, JWT contract test, identity contract` — full implementation; all gates pass.
2. `demo: revert MSAL scope to User.Read — gate must fail` — single-line change to `frontend/src/lib/msal-config.ts`. CI on this commit MUST fail with `Forbidden MSAL scope literal "User.Read"`. Link the failing GH Actions run from the PR body.
3. `revert: restore correct MSAL scope (demo verified above)` — straight revert of commit 2.

The reviewer (Pablo, then any future contributor) sees three CI runs in the PR history. The middle one is RED with the structured diagnostic.

**Why we don't ALSO have a negative-control for Gates 2/3:** Each gate has its own internal test fixtures (RuleTester for G1, parametrized cases for G2, drift fixtures for G3) that exercise the failure path. The PR-level negative-control covers Gate 1 specifically because that's the **literal May 20 bug pattern** — its proof is most meaningful as a real CI run. Gate 2 and 3 are validated by their fixture coverage; running a negative-control commit for each would triple the demo-commit churn for diminishing returns.

## 10. Testing Strategy

| Test layer                         | What it covers                                             | Where                                                                |
| ---------------------------------- | ---------------------------------------------------------- | -------------------------------------------------------------------- |
| **Unit (existing)**                | `validate_token` exception classes (extended for scp/ver)  | `backend/tests/unit/test_auth.py` (updated)                          |
| **Integration: auth contract**     | 11 parametrized cases through real FastAPI dependency      | `backend/tests/integration/test_auth_contract.py` (NEW — Gate 2)     |
| **Integration: identity contract** | Schema validation + cross-file drift detection             | `backend/tests/integration/test_identity_contract.py` (NEW — Gate 3) |
| **Frontend ESLint rule tests**     | RuleTester valid/invalid fixtures for `msai/msal-scopes-*` | `frontend/eslint-rules/__tests__/msal-scopes.test.ts` (NEW — Gate 1) |
| **PR-level negative-control**      | Real CI failure on `User.Read` reintroduction              | Commit history of THIS PR                                            |

Coverage gap explicitly accepted (PRD §2 non-goals): no live Entra integration. That's the deferred `deploy.yml` Bearer probe PR.

## 11. Resolved Open Questions (PRD §7 + research §3-9)

| PRD §7 question                                                           | Resolution                                                                                                                                                 |
| ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Gate 1 implementation: ts-morph vs ESLint vs TS Compiler API              | **ESLint custom rule via `@typescript-eslint/utils`** (research §1 Option A; canonical 2026 pattern, co-located with frontend toolchain).                  |
| Backend auth middleware test scaffolding: how to hit the real dependency? | `app.dependency_overrides.pop(get_current_user)` in module-scoped fixture + `ASGITransport` + httpx.AsyncClient (research §4).                             |
| JWKS patching mechanism                                                   | Inject `validator._jwks_client = mock` after `init_validator()`; mock implements multi-`kid` lookup with PyJWKClientError on unknown kid.                  |
| `identity-contract.json` location                                         | **Repo root.** Sibling `identity-contract.README.md` explains the convention.                                                                              |
| Should `identity-contract.json` be consumed at RUNTIME?                   | **No.** Lint-time + test-time only. Backend reads env vars via pydantic-settings; frontend reads `NEXT_PUBLIC_*` at build. Avoids browser-bundle coupling. |
| Token-version (`ver`) handling                                            | Backend expects `ver=="2.0"`. Extended `validate_token` enforces it explicitly (new custom check).                                                         |
| Cross-platform (Linux CI + macOS dev)                                     | All gates use cross-platform tooling (Node + Python). No shell-script specifics that differ across OS.                                                     |
| Research Open Risk #1: extending `validate_token` for scp/ver             | **Extend it.** In scope per NO BUGS LEFT BEHIND + Codex's pre-pivot endorsement. ~10 LoC. Has no negative downstream effect on the API-key path.           |
| Research Open Risk #2: template literal scope resolution                  | **Known-safe template shape.** Rule accepts `api://${KNOWN_ENV_REFERENCE}/${LITERAL_SCOPE_NAME_MATCHING_CONTRACT}`. Anything else is reported.             |
| Research Open Risk #8: confusion between JWT path and API-key path        | Gate 2 failure messages prefix with "JWT validation regression"; never bare "auth regression". Documented in `auth-gate.md` runbook.                       |

## 12. Out-of-Scope (Explicit)

Anything not in §§ 4-8 above is out of scope for THIS PR. In particular:

- WIF-backed Entra metadata check (Codex suggested, deferred per PRD §2)
- Headless Playwright real-browser flow (deferred per PRD §2)
- ROPC anywhere (dropped per PRD §1 pivot)
- `deploy.yml` post-deploy Bearer probe (separate ~30 min PR in `state.md ## Next`)
- E2E ingest→backtest smoke test (separate ~2-3h PR in `state.md ## Next`)
- Multi-tenant / multi-account fleet support for the contract (single-tenant contract is sufficient v1; future PR can add per-tenant arrays)
- WebSocket auth handshake separate UC (transitively covered by Gate 2 contract test on `validate_token`)
- Removing or restricting the `X-API-Key` path (PRD non-goal; deliberate dual-auth design)

## 13. Spec self-review

| Check                | Result                                                                                                                           |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Placeholders         | None. `<GUID>` placeholders in example identity-contract.json are illustrative (real values land via Pablo's commit).            |
| Internal consistency | Gate 2 cases align with backend `validate_token` extension (§6). identity-contract fields used by Gate 1 (§4) match schema (§3). |
| Scope                | Single PR. ~14 files touched. ~2-4 hours implementation. Inside scope of `/new-feature`.                                         |
| Ambiguity            | None. Every gate has a concrete file path, library choice, and test strategy. Every PRD §7 question is resolved (§11).           |

**Approved for `/council` contrarian gate.**
