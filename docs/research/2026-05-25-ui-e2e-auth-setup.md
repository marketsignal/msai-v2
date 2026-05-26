# Research: ui-e2e-auth-setup (auth-regression-gate)

**Date:** 2026-05-25
**Feature:** Three deterministic PR-time gates (MSAL scope AST lint, backend PyJWT contract test with synthetic JWT, identity-contract.json cross-file consistency lint) to catch the May 20 MSAL outage class without any live Entra dependency.
**Researcher:** research-first agent
**PRD:** `docs/prds/ui-e2e-auth-setup.md` (v2.0)

---

## Libraries Touched

| Library                       | Our Version        | Latest Stable | Breaking Changes Since Ours                    | Source                                                                                                                              |
| ----------------------------- | ------------------ | ------------- | ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `@azure/msal-browser`         | ^5.3.0             | 5.10.1        | None affecting scope arrays                    | [CHANGELOG](https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-browser/CHANGELOG.md) (2026-05-25) |
| `@azure/msal-react`           | ^5.0.5             | ~5.x          | None affecting scope flow                      | [npm](https://www.npmjs.com/package/@azure/msal-react) (2026-05-25)                                                                 |
| `eslint`                      | ^9                 | 9.x (flat)    | n/a (we ship 9 already)                        | [migration](https://eslint.org/docs/latest/use/configure/migration-guide) (2026-05-25)                                              |
| `typescript`                  | ^5                 | 5.x           | n/a                                            | (no upgrade pressure)                                                                                                               |
| `@typescript-eslint/utils`    | NEW dep            | latest        | n/a                                            | [custom rules](https://typescript-eslint.io/developers/custom-rules) (2026-05-25)                                                   |
| `ts-morph` (alternative)      | NEW dep            | 28.0.0        | n/a                                            | [npm](https://www.npmjs.com/package/ts-morph) (2026-05-25)                                                                          |
| `PyJWT[crypto]`               | >=2.9.0            | 2.13.0        | **Security-tightening only — no API breakage** | [changelog](https://pyjwt.readthedocs.io/en/latest/changelog.html) (2026-05-25)                                                     |
| `cryptography`                | >=43.0.0           | 44+           | None for RSA keypair use                       | (used today in `tests/unit/test_auth.py`)                                                                                           |
| `httpx`                       | >=0.28.0           | 0.28+         | `ASGITransport` is stable                      | [FastAPI async tests](https://fastapi.tiangolo.com/advanced/async-tests/) (2026-05-25)                                              |
| `fastapi`                     | >=0.133.0          | 0.136.3       | None affecting `Depends()` / `ASGITransport`   | [releases](https://github.com/fastapi/fastapi/releases) (2026-05-25)                                                                |
| `pytest` (+ `pytest-asyncio`) | >=8.3.0 / >=0.24.0 | n/a           | n/a                                            |                                                                                                                                     |

> No version is out of date for this feature. The two **new** dependencies the design phase must choose between are `@typescript-eslint/utils` (for an ESLint custom rule) or `ts-morph` (for a standalone CI script). Either can implement Gate 1.

---

## Per-Library Analysis

### 1. TypeScript AST inspection — `@typescript-eslint/utils` vs `ts-morph` vs raw TS Compiler API

**Versions:** `@typescript-eslint/utils` is the canonical lib for typed ESLint rules in 2026 (flat config compatible). `ts-morph` is at 28.0.0 (released 2026-04-12), still actively maintained.

**Recommended pattern (for THIS feature):**

There are three viable approaches:

| Option                                                   | Setup                                                                                                   | When it fires                       | Trade-offs                                                                                                                                                                                           |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- | ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A. Custom ESLint rule via `@typescript-eslint/utils`** | Add a plugin under `frontend/eslint-rules/msal-scopes.ts`, register in `eslint.config.js` (flat config) | On every `pnpm lint`, in IDE, in CI | Tightest IDE feedback. Co-located with rest of frontend lint. Recommended pattern at `typescript-eslint.io/developers/custom-rules`. Works with ESLint 9 flat config via `defineConfig` + `plugins`. |
| **B. Standalone `ts-morph` script**                      | Add a Node script under `scripts/lint-msal-scopes.ts`; CI runs `tsx scripts/lint-msal-scopes.ts`        | Only when explicitly invoked in CI  | Less framework overhead. Loses IDE squiggles. Adds a new top-level toolchain.                                                                                                                        |
| **C. Raw TypeScript Compiler API**                       | Hand-rolled `ts.createProgram` + visitor                                                                | Same as B                           | Most boilerplate, hardest to maintain. `@typescript-eslint/utils` and `ts-morph` both wrap this. Skip.                                                                                               |

**Pattern for inspecting scope literals (option A — recommended):**

```typescript
import { ESLintUtils, TSESTree } from "@typescript-eslint/utils";

const createRule = ESLintUtils.RuleCreator((name) => `https://msai/${name}`);

export default createRule({
  name: "msal-no-graph-scopes",
  meta: {
    type: "problem",
    schema: [],
    docs: { description: "..." },
    messages: { forbidden: "{{file}}:{{line}}: forbidden scope {{value}}" },
  },
  defaultOptions: [],
  create(context) {
    const allowedStandardScopes = new Set([
      "openid",
      "profile",
      "email",
      "offline_access",
    ]);
    const isApiScope = (s: string) => /^api:\/\/[^/]+\/[\w.-]+$/.test(s);
    const isForbidden = (s: string) =>
      s.includes("User.Read") ||
      s.includes("graph.microsoft.com") ||
      (!allowedStandardScopes.has(s) && !isApiScope(s));

    return {
      // Inspects every property literal whose key is "scopes"
      'Property[key.name="scopes"] > ArrayExpression'(
        node: TSESTree.ArrayExpression,
      ) {
        for (const el of node.elements) {
          // Literal: "User.Read"
          if (
            el?.type === "Literal" &&
            typeof el.value === "string" &&
            isForbidden(el.value)
          ) {
            context.report({
              node: el,
              messageId: "forbidden",
              data: { value: el.value },
            });
          }
          // TemplateLiteral: `api://${id}/access_as_user`
          if (el?.type === "TemplateLiteral") {
            // resolve to identity-contract.json values; if cannot resolve statically, fail closed
          }
        }
      },
    };
  },
});
```

**Discovered gotcha — `@typescript-eslint/utils` discourages dual-mode rules.** Per docs: _"We recommend AGAINST changing rule logic based solely on whether `services.program` exists."_ So if Gate 1 needs type information (it doesn't strictly — string-literal value inspection is enough), gate type-aware behavior behind an explicit option rather than auto-detecting.

**Sources:**

1. [typescript-eslint custom rules guide](https://typescript-eslint.io/developers/custom-rules) — accessed 2026-05-25 (current recommended pattern)
2. [ESLint 9 flat config migration](https://eslint.org/docs/latest/use/configure/migration-guide) — accessed 2026-05-25 (covers `defineConfig`, `plugins` registration)
3. [ts-morph navigation API](https://ts-morph.com/navigation/) — accessed 2026-05-25 (alternative path — `Project.getSourceFiles().forEachDescendant(node => Node.isCallExpression(node)...)`)
4. [ts-morph npm v28.0.0](https://www.npmjs.com/package/ts-morph) — accessed 2026-05-25

**Design impact:** Pick **Option A (custom ESLint rule)** as the default. It (1) reuses the existing `pnpm lint` invocation already in `frontend/package.json`, (2) gives IDE feedback during development, (3) is the canonical pattern in 2026. Reserve `ts-morph` as fallback only if discovery during design surfaces a case the ESLint AST can't reach (e.g., needing to follow imports across files for template-literal resolution — possible but more complex in an ESLint rule).

**Test implication:** The custom rule itself needs unit tests via `RuleTester` from `@typescript-eslint/rule-tester` (the canonical test harness). Add positive + negative-case fixtures: `"User.Read"`, `"https://graph.microsoft.com/User.Read"`, `"api://abc-123/access_as_user"`, `api://${CLIENT_ID}/access_as_user`. Each fixture must assert the exact `messageId` + report node location. **This is the test of the test** — without it, a bug in the rule (e.g. typo'd regex) would let real Graph scopes slip through.

---

### 2. `@azure/msal-browser` v5+ scope-carrying API surface

**Versions:** ours = ^5.3.0, latest = 5.10.1 (2026-05-11). Our pin floats `^5` so `pnpm install` already pulls newer minor. No breaking changes affect this feature.

**API shapes where `scopes: string[]` appears (these are the call sites Gate 1 must cover):**

| Interface / Method                                                 | Where in our code                                                                                  |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| `PopupRequest.scopes` (`loginPopup`, `acquireTokenPopup`)          | Not currently called (we use redirect). Gate 1 should still cover for future use.                  |
| `RedirectRequest.scopes` (`loginRedirect`, `acquireTokenRedirect`) | `frontend/src/lib/auth.ts:62` — `instance.loginRedirect(loginRequest)`                             |
| `SilentRequest.scopes` (`acquireTokenSilent`, `ssoSilent`)         | `frontend/src/lib/auth.ts:72` — `instance.acquireTokenSilent({ ...loginRequest, account })`        |
| `EndSessionRequest` (logout)                                       | `frontend/src/lib/auth.ts:65` — no scopes (logout doesn't carry scopes; Gate 1 should not flag).   |
| **Exported scope arrays / objects**                                | `frontend/src/lib/msal-config.ts:42` — `export const loginRequest = { scopes: [...] }`             |
| `protectedResourceMap`                                             | Not currently used. Was a legacy MSAL Angular pattern, not a primary `msal-browser` concern in v5. |

**v4 → v5 changes that are NOT relevant to this gate** (per `learn.microsoft.com/.../v4-migration`):

- `handleRedirectPromise` signature change → only affects redirect callback wiring, not scopes
- `enableAccountStorageEvents` removal → unrelated
- `getAccountByHomeId/LocalId/Username` → replaced by `getAccount({...})`, irrelevant to scopes
- `PublicClientNext` removal → standardised on `PublicClientApplication`; we already use that
- `protocolMode` moved from `BrowserAuthOptions` → `SystemOptions` — irrelevant
- `navigatePopups` rename + reversed semantics — irrelevant to scopes
- `extraQueryParameters`/`tokenBodyParameters`/`tokenQueryParameters` consolidated into single `extraParameters` — irrelevant unless we start passing them
- COOP / redirect-bridge added in v5 — separate workstream (out of scope here)

**`scopes: string[]` shape is unchanged from v4 to v5.** Microsoft's canonical recommendation in 2026 for a SPA calling its OWN backend is still:

```typescript
{
  scopes: ["openid", "profile", "email", "api://<client-id>/<scope-name>"];
}
```

with `access_as_user` as the conventional scope-name. (Confirmed by Microsoft Q&A: `"api://xxxxxxx-xxxxxxx-xxxxxxxx-xxxxxxxx/access_as_user"`, scope token returned with `"scp": "access_as_user"`.) The current `msal-config.ts` already matches this.

**Sources:**

1. [MSAL Browser v4→v5 migration](https://learn.microsoft.com/en-us/entra/msal/javascript/browser/v4-migration) — accessed 2026-05-25
2. [msal-browser CHANGELOG (5.10.1)](https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-browser/CHANGELOG.md) — accessed 2026-05-25
3. [Application & Delegated Permissions for Access Tokens](https://learn.microsoft.com/en-us/troubleshoot/entra/entra-id/app-integration/application-delegated-permission-access-tokens-identity-platform) — accessed 2026-05-25 (confirms `access_as_user` is current canonical scope name)

**Design impact:** Gate 1 must scan for `scopes:` keys on EVERY object literal passed to these MSAL methods, plus exported objects whose name ends in `Request` or matches `loginRequest`/`tokenRequest` patterns. The discovery should be **import-graph driven** (any file that imports from `@azure/msal-browser` or `@azure/msal-react` is in scope) — NOT a hardcoded path list — so a future `lib/auth.ts`-style helper or new MSAL wrapper file is automatically covered.

**Test implication:** The Gate 1 fixture suite must include each call site shape: bare object literal at call site, spread (`{ ...loginRequest, account }` — our current pattern), exported `loginRequest` constant, and template-literal-in-scope case (the one already in our code at line 47: `api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID || ""}/access_as_user`). The spread case is **critical** — Gate 1 must follow the spread to the source object, OR (simpler) require that every spread-merged object is also one of the inspected exports.

---

### 3. PyJWT 2.13.0 — synthetic JWT contract testing patterns

**Versions:** ours = `PyJWT[crypto]>=2.9.0` (pyproject.toml). Latest = 2.13.0 (May 2026). **No breaking changes** in `PyJWKClient`, audience validation, or `InvalidAudienceError`/`InvalidIssuerError` since 2.9.0 — all changes 2.10–2.13 are security-tightening (e.g., `iss` claim partial-matching prevention, JWK-as-HMAC-secret rejection).

**Exception class hierarchy** (verified against PyJWT source — `jwt/exceptions.py`):

```
Exception
  └─ PyJWTError
       └─ InvalidTokenError
            ├─ InvalidAudienceError          (aud mismatch)
            ├─ InvalidIssuerError            (iss mismatch)
            ├─ ExpiredSignatureError         (exp in past)
            ├─ ImmatureSignatureError        (nbf in future)
            ├─ DecodeError
            │    └─ InvalidSignatureError    (wrong key signed it)
            ├─ InvalidIssuedAtError          (iat malformed)
            ├─ InvalidAlgorithmError         (alg not in whitelist)
            └─ MissingRequiredClaimError     (`options={"require": [...]}`)
       └─ PyJWKClientError                   (JWKS fetch/parse — kid miss inherits InvalidKey / InvalidTokenError)
       └─ InvalidKeyError                    (key shape problem)
```

**Important:** `EntraIDValidator.validate_token` in our `auth.py` catches `jwt.InvalidTokenError` (line 124 of `auth.py`). That's the **parent** class — all subclass exceptions are caught. Gate 2's tests should still assert the SPECIFIC subclass per negative case (using `pytest.raises(InvalidAudienceError)`, etc.) so a future change to the validator's narrowness is immediately visible.

**JWKS patching mechanism — patterns in current codebase:**

The existing `backend/tests/unit/test_auth.py` already uses the recommended pattern:

```python
# Generate ephemeral RSA keypair per test (cryptography library)
private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
public_pem = private_key.public_key().public_bytes(...)

# Mock PyJWKClient — return a stand-in signing-key object
mock_client = MagicMock()
mock_signing_key = MagicMock()
mock_signing_key.key = public_pem
mock_client.get_signing_key_from_jwt.return_value = mock_signing_key

# Inject directly on the validator instance
validator = EntraIDValidator(TENANT_ID, CLIENT_ID)
validator._jwks_client = mock_client
```

This pattern works because `EntraIDValidator.validate_token` calls `self._jwks_client.get_signing_key_from_jwt(token)` and then passes `signing_key.key` to `jwt.decode`. The mock returns a `key` attribute holding the PEM bytes, which PyJWT accepts directly as an RSA public key.

**Limitations of the current pattern** (Gate 2 must address):

- It instantiates `EntraIDValidator(TENANT_ID, CLIENT_ID)`, which constructs a real `PyJWKClient` pointing at `login.microsoftonline.com/.../discovery/v2.0/keys`. **This DOES make a network connection on construction** (the `PyJWKClient` constructor doesn't fetch, but tests must assert no fetch happens). Gate 2 should patch _at construction time_ — either via dependency injection of the JWKS client, OR by monkeypatching `PyJWKClient` for the test module before importing `auth.py`.
- The mock pattern hides the `kid` lookup logic — Gate 2's "bad kid" negative case (PRD US-002 case 6) needs the mock to actually `raise PyJWKClientError` when called with an unknown kid. The current pattern blindly returns one key.

**Sources:**

1. [PyJWT 2.13.0 docs — Usage Examples](https://pyjwt.readthedocs.io/en/latest/usage.html) — accessed 2026-05-25
2. [PyJWT 2.13.0 changelog](https://pyjwt.readthedocs.io/en/latest/changelog.html) — accessed 2026-05-25
3. [PyJWT exceptions.py source](https://github.com/jpadilla/pyjwt/blob/master/jwt/exceptions.py) — accessed 2026-05-25
4. [Mocking JWKS in pytest pattern](https://fmpm.dev/mocking-auth0-tokens) — accessed 2026-05-25

**Design impact:** Gate 2 should EXTEND the existing pattern with:

1. A network-call-blocking guard (e.g., autouse fixture that monkeypatches `urllib.request.urlopen` and `httpx.get`/`httpx.AsyncClient.get` to raise `RuntimeError("Network blocked in test")`). The PRD requires "no network calls during gate execution"; verifying it requires _provably_ blocking the network, not just trusting the mock.
2. A multi-`kid` JWKS mock — the real Entra JWKS has multiple keys; the test JWKS should also have multiple, with the unknown-kid case raising a real `PyJWKClientError`-shaped error.
3. Per-negative-case **specific** exception assertions (e.g., `InvalidAudienceError` for Graph-aud, `InvalidIssuerError` for wrong tenant, `ExpiredSignatureError` for expired, `MissingRequiredClaimError` for missing-scp scenarios via `options.require`).

**Test implication:** Each of the 9 PRD negative cases maps to a specific PyJWT exception:

| Case | Negative                          | Specific PyJWT exception                                                          |
| ---- | --------------------------------- | --------------------------------------------------------------------------------- |
| 1    | `aud=https://graph.microsoft.com` | `InvalidAudienceError`                                                            |
| 2    | Wrong tenant in `iss`             | `InvalidIssuerError`                                                              |
| 3    | Missing `scp` claim               | `MissingRequiredClaimError` (if `options.require` includes `scp`) OR custom check |
| 4    | `scp=User.Read`                   | Custom check (PyJWT does NOT validate scope content)                              |
| 5    | Expired (`exp` past)              | `ExpiredSignatureError`                                                           |
| 6    | Bad `kid`                         | `PyJWKClientError` (raised by JWKS client)                                        |
| 7    | Wrong signature                   | `InvalidSignatureError`                                                           |
| 8    | App-only `roles` claim, no `scp`  | Custom check (PyJWT doesn't gate on this)                                         |
| 9    | v1.0 issuer (`sts.windows.net`)   | `InvalidIssuerError` (because backend expects v2.0 URL)                           |

**Critical finding:** Cases 3, 4, 8 are NOT enforced by stock PyJWT — our `validate_token` only requires `["exp", "iss", "aud", "sub"]` (auth.py:54). If we want Gate 2 to fail on these, **the backend `validate_token` must be extended** to validate `scp` claim presence + content. This is a code change required to make Gate 2 testable as US-002 specifies. The PRD's "No Bugs Left Behind" policy makes this in-scope.

---

### 4. FastAPI 0.133+ dependency-injection testing with `ASGITransport`

**Versions:** ours = `fastapi>=0.133.0`, latest = 0.136.3 (2026-05-23). `httpx>=0.28.0`, `ASGITransport` API has been stable since httpx 0.27.

**Current best practice (2026):**

```python
import httpx
from httpx import ASGITransport
from msai.main import app

@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
```

**Our codebase already uses this pattern.** `backend/tests/conftest.py:61-65`:

```python
@pytest.fixture
def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")
```

The existing pattern is incomplete for Gate 2 because:

1. The autouse fixture `_override_auth` (conftest.py:49-58) **bypasses `get_current_user` entirely** with a static dict. Gate 2 needs the REAL dependency to run (that's what's being tested). The Gate 2 test module must clear that override OR opt out via a marker.
2. The existing `test_auth.py` uses `TestClient` (sync) not `AsyncClient`. That's fine for a single-request contract test, but the PRD's "hits the real backend auth middleware" requirement is satisfied either way — both routes go through `Depends(get_current_user)`.

**Dependency override pattern for the positive case** (in Gate 2):

```python
# Don't override get_current_user — the test mints a real JWT that the
# real EntraIDValidator (with patched JWKS) accepts.
app.dependency_overrides.pop(get_current_user, None)
# Install the validator that our test owns
auth_module._validator = test_validator_with_mocked_jwks
# Drive the request through ASGITransport — exercises full FastAPI lifecycle
async with httpx.AsyncClient(transport=ASGITransport(app=app), ...) as ac:
    resp = await ac.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
```

**Caveat from FastAPI 2026 docs (`fastapi.tiangolo.com/advanced/async-tests/`):** `ASGITransport` does NOT trigger lifespan events. For a test that only exercises the request lifecycle (which Gate 2 does), this is irrelevant. If a future need touches startup hooks, use `asgi-lifespan.LifespanManager`.

**Sources:**

1. [FastAPI Async Tests guide](https://fastapi.tiangolo.com/advanced/async-tests/) — accessed 2026-05-25
2. [FastAPI testing discussion #11785](https://github.com/fastapi/fastapi/discussions/11785) — accessed 2026-05-25
3. [Testing FastAPI Endpoints Without Spinning Up a Server](https://dev.to/peytongreen_dev/testing-fastapi-endpoints-without-spinning-up-a-server-j74) — accessed 2026-05-25

**Design impact:**

- Gate 2 should target `/api/v1/auth/me` (the simplest authenticated endpoint — line 1 of every router that does `Depends(get_current_user)`), not a deeper handler that involves a DB session.
- The test module **must** locally clear/reinstall the autouse `_override_auth` override OR use a dedicated `pytest.fixture` scope to wire the real validator + mocked JWKS. The cleanest pattern is a session-scoped fixture in `backend/tests/integration/test_auth_contract.py` that clears the override at module scope, runs all 9 negatives + 1 positive, then restores.
- This is a NEW test file (`backend/tests/integration/test_auth_contract.py`) — co-locating with the existing `tests/unit/test_auth.py` would entangle the autouse override semantics.

**Test implication:** Each negative case becomes an `httpx.AsyncClient.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {bad_token}"})` that asserts `response.status_code == 401` and `response.json()["detail"]` names the failing claim. Use `pytest.mark.parametrize` over the 9 negatives + 1 positive to keep AAA-clean.

---

### 5. Microsoft Entra v2.0 access token claims contract (current)

**Source of truth:** `learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference` (last updated 2025-10-02). No structural changes in the claim shapes through 2026-05.

**Delegated-user token (issued to SPA via auth-code + PKCE) — example payload that the positive case must mint:**

```json
{
  "aud": "<client-id-GUID>",
  "iss": "https://login.microsoftonline.com/<tenant-id>/v2.0",
  "iat": 1537231048,
  "nbf": 1537231048,
  "exp": 1537234948,
  "azp": "<client-id-GUID>",
  "azpacr": "0",
  "name": "Abe Lincoln",
  "oid": "<user-oid-GUID>",
  "preferred_username": "abeli@microsoft.com",
  "scp": "access_as_user",
  "sub": "<pairwise-sub-string>",
  "tid": "<tenant-id-GUID>",
  "uti": "<token-id>",
  "ver": "2.0"
}
```

Header:

```json
{ "typ": "JWT", "alg": "RS256", "kid": "<rotating-kid>" }
```

**App-only token (client_credentials) — has `roles` instead of `scp`:**

```json
{
  "aud": "<client-id>", "iss": "...", "exp": ..., "iat": ...,
  "azp": "<client-id>", "azpacr": "1" or "2",
  "roles": ["MyApi.Read"],
  "ver": "2.0"
  // NO scp claim, NO oid for a person, sub == oid of the SP
}
```

**Issuer format (v2.0 vs v1.0):**

- **v2.0:** `https://login.microsoftonline.com/<tenant-id>/v2.0` — current backend expects exactly this (auth.py:38).
- **v1.0:** `https://sts.windows.net/<tenant-id>/` — DIFFERENT HOST. Negative case 9 in PRD US-002 covers this.

**`kid` header:** Rotates on Microsoft's schedule; our `PyJWKClient` cache lifespan is 300s (auth.py:43). The `kid` is the only field that selects the right key from the JWKS document. **v2.0 tokens contain `kid` only; v1.0 tokens contain both `kid` and `x5t`.**

**`ver` claim:** Mandatory. Backend should reject `ver != "2.0"` for the user path. Currently the backend does NOT check `ver` explicitly — but a v1.0 token would fail `iss` validation anyway (different host). However, an attacker controlling a v2.0-formatted token with `ver: "1.0"` would still pass. Gate 2 case 9 surfaces this.

**`scp` claim shape:** **String — space-separated**, not a JSON array. So `"scp": "access_as_user openid profile"` is one claim with three values. The backend's `validate_token_or_api_key` does not parse `scp` today — it would need to split-on-space and check for the expected scope value if Gate 2 case 4 is to fail.

**2025/2026 changes:** None to the claim shapes themselves. Microsoft made one Entra _operational_ change (mandatory MFA enforcement) that's covered in the PRD's v1→v2 pivot.

**Sources:**

1. [Access token claims reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference) — accessed 2026-05-25 (last updated 2025-10-02)
2. [Access tokens overview](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens) — accessed 2026-05-25 (last updated 2025-10-02)
3. [Microsoft identity platform claims validation](https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation) — referenced from PRD
4. [SCP / access_as_user pattern (Microsoft Q&A)](https://learn.microsoft.com/en-gb/answers/questions/5632841/clarification-on-scp-vs-roles-claims-in-client-cre) — accessed 2026-05-25

**Design impact:**

- The positive-case mint should look EXACTLY like Microsoft's documented v2.0 example: `scp` is a space-separated string `"access_as_user"`, `ver: "2.0"`, `iss` host = `login.microsoftonline.com`.
- The 9 negative cases each manipulate ONE claim. Tests must be parametrized so failures point at the specific manipulation.
- Backend `validate_token` currently does NOT enforce `scp` content or `ver` — extending it is in-scope (NO BUGS LEFT BEHIND).

**Test implication:** Add a `make_entra_token(**overrides)` helper in `tests/integration/test_auth_contract.py` that builds the canonical positive payload and accepts overrides for each negative case. This mirrors the `_make_token` helper already in `tests/unit/test_auth.py` but extends it for `scp`, `ver`, `roles`, `azp`, `azpacr`.

---

### 6. `identity-contract.json` — single-source-of-truth identity config

**Versions:** N/A — there is **no established convention** in Microsoft / MSAL / Azure SDK / known FOSS projects for a project-internal "identity contract" file. The closest analogs are:

- OIDC `.well-known/openid-configuration` (consumed at runtime, server-side concept — not project-internal)
- `appsettings.json` / `web.config` in .NET ecosystems (loose convention, not standardized)
- `.env.example` (de facto, but only key names, not values — and brittle to drift)

**Recommended schema for our use case** (informed by Entra's claim contract):

```json
{
  "$schema": "https://msai.local/identity-contract.schema.json",
  "tenant_id": "<GUID>",
  "client_id": "<GUID>",
  "app_id_uri": "api://<client-id-GUID>",
  "issuer": "https://login.microsoftonline.com/<tenant-id>/v2.0",
  "scope_name": "access_as_user",
  "token_version": "2.0",
  "frontend_env_prefix": "NEXT_PUBLIC_",
  "env_var_names": {
    "tenant_id": "AZURE_TENANT_ID",
    "client_id": "AZURE_CLIENT_ID",
    "jwt_tenant_id": "JWT_TENANT_ID",
    "jwt_client_id": "JWT_CLIENT_ID"
  }
}
```

**Why a JSON Schema reference:** Pydantic / TypeScript both have first-class JSON Schema support. The contract becomes machine-readable, IDE auto-complete works on it, and Gate 3 can validate file shape before doing the cross-file check.

**Why a separate `env_var_names` block:** so Gate 3 can discover env-var references (including the `NEXT_PUBLIC_` prefix variant) without hardcoding them. New env vars added in future config files get caught when their canonical names are in this block.

**Should it be consumed at RUNTIME?** PRD §7 open question. Findings:

- **Backend:** Already reads `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` via pydantic-settings. Switching to JSON would require parallel-loading + falling back. Cost > benefit for tenant/client IDs (they're stable per env).
- **Frontend:** Next.js needs `NEXT_PUBLIC_*` for client bundle. Cannot read JSON at runtime in the browser without baking it into the build. So `NEXT_PUBLIC_*` env vars are still required.
- **Recommendation:** Keep `identity-contract.json` as a **lint-time + test-time source of truth ONLY**. Runtime stays on env vars. Gate 3 enforces the env vars match the contract. This avoids the runtime/build-time coupling problem.

**Sources:**

1. [OAuth 2.0 client configuration patterns (SSOJet)](https://ssojet.com/blog/oauth-authorization-server-setup-implementation-guide-configuration) — accessed 2026-05-25 (background reading; no specific convention found)
2. [Microsoft identity platform glossary](https://docs.azure.cn/en-us/entra/identity-platform/developer-glossary) — accessed 2026-05-25 (canonical names for fields)
3. _No formal industry convention located._ Confirmed by direct WebSearch on `"identity-contract.json"` — zero hits in OSS projects (2026-05-25).

**Design impact:**

- We are **inventing** this convention. Document its purpose clearly at the top of the JSON file via `_comment` or in a sibling `README.md`. Future contributors should not assume this is a standard.
- Schema validation should run BEFORE Gate 3's cross-file check — if the contract itself is malformed, the cross-file check is meaningless.
- File location decision (PRD §7 open): I recommend **repo root** (`./identity-contract.json`) for visibility. `config/identity-contract.json` is the second-best alternative if we want to add more identity-related contracts later (e.g., for a separate test tenant).

**Test implication:** Gate 3 is a NEW Python script (or Node script — language choice is design-phase). It needs unit tests of its own with fixtures showing PASS (all files in sync) and FAIL (one file drifted). The lint must also have an autodiscovery test — add a new file with a canonical env-var name in a fixture repo and assert it's picked up.

---

### 7. GitHub Actions — running ESLint custom rule + pytest + cross-file lint in parallel

**2026 best practices:**

- Use a single workflow file (`.github/workflows/auth-gate.yml`) with **three parallel jobs** (one per gate). Each job specifies `needs:` if there's a dependency (none here — gates are independent).
- Cache `pnpm` store via `setup-node`'s built-in cache OR `pnpm/action-setup@v4` + `actions/cache@v4` keyed by `pnpm-lock.yaml`. Real-world tests show ~50%+ install-time reduction.
- Cache `uv` via `setup-python` + the `uv` CLI's lock-file-aware caching (`uv sync --locked`).
- Avoid `fail-fast: false` on matrices for THIS workflow — we WANT a failure in any gate to fail the PR.

**Skeleton job structure (informed by `oneuptime.com/.../github-actions-parallel-tests`):**

```yaml
name: Auth Regression Gate
on:
  pull_request:
    branches: [main]

jobs:
  gate-1-msal-scope-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
        with: { version: 9 }
      - uses: actions/setup-node@v4
        with:
          {
            node-version: 20,
            cache: "pnpm",
            cache-dependency-path: frontend/pnpm-lock.yaml,
          }
      - working-directory: frontend
        run: pnpm install --frozen-lockfile
      - working-directory: frontend
        run: pnpm lint # custom rule fires here

  gate-2-jwt-contract:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with: { enable-cache: true }
      - working-directory: backend
        run: uv sync --locked --extra dev
      - working-directory: backend
        run: uv run pytest tests/integration/test_auth_contract.py -v

  gate-3-identity-contract:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # Use whichever runtime the design phase picks (python via uv OR node via pnpm)
      - run: ./scripts/lint-identity-contract.{py|ts}
```

**Sources:**

1. [GitHub Actions parallel tests (oneuptime)](https://oneuptime.com/blog/post/2025-12-20-github-actions-parallel-tests/view) — accessed 2026-05-25
2. [Monorepo CI patterns (WarpBuild)](https://www.warpbuild.com/blog/github-actions-monorepo-guide) — accessed 2026-05-25
3. [Matrix builds practical guide](https://eastondev.com/blog/en/posts/dev/20260428-github-actions-matrix/) — accessed 2026-05-25

**Design impact:**

- Three sibling jobs, not a matrix — different toolchains per job (`pnpm` vs `uv` vs choice).
- The PRD's "≤ 5 seconds total wall-clock" target is achievable per-job: each runs in isolation with cached deps. Total billable time may be 3× (parallel jobs run on separate runners), but **wall-clock** is bounded by the slowest gate.
- Required-check rule: add all three gate jobs to the branch protection's required status checks for `main`.

**Test implication:** Each gate must produce machine-readable output on failure (file:line:value structure per US-001/002/003 acceptance criteria) so the GH Actions log is immediately diagnosable without re-running the gate locally.

---

### 8. MSAL.js v5 scope-array — Microsoft's current (2026) canonical SPA shape

**Microsoft's recommendation** (cross-referenced across the migration guide, claims docs, and Q&A threads):

```typescript
export const loginRequest = {
  scopes: ["openid", "profile", "email", "api://<client-id>/access_as_user"],
};
```

Where `access_as_user` is the **exposed scope name** in the Entra app registration's "Expose an API" blade. The name is conventional; the project could choose `read` or `api.access`, etc., but `access_as_user` is the documented canonical default.

**Why this shape is correct** (recap from our post-mortem and Microsoft docs):

- `openid`, `profile`, `email` → OIDC reserved scopes for ID-token claims (name, email). MSAL.js strips these before the resource access-token request. No effect on `aud`.
- `api://<client-id>/<scope-name>` → resource scope. Token issued has `aud=<client-id>` and `scp=<scope-name>`. PyJWT can decode.

**Anti-patterns Microsoft explicitly warns against:**

1. **`User.Read`** alone → returns a Microsoft Graph token (`aud=00000003-0000-0000-c000-000000000000`). Opaque to third parties. This was the May 20 outage.
2. **`.default` on a SPA → own-API call** → per `learn.microsoft.com/.../scopes-oidc`: "new clients shouldn't use that setup." Returns an ID token in place of an access token under some conditions. `.default` is for static admin-consent flows (OBO, client-creds), not interactive SPA → own-API. This is what shipped in PR #74 and caused AADSTS500011.
3. **Mixed-resource scope arrays** → e.g., `["api://abc/scope", "User.Read"]`. Causes AADSTS28000 (resources may only be a single resource per request). Even when accepted, the issued token's `aud` is unpredictable.

**Sources:**

1. [Access tokens overview](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens) — accessed 2026-05-25
2. [MSAL Browser v4-v5 migration (no scope-shape changes)](https://learn.microsoft.com/en-us/entra/msal/javascript/browser/v4-migration) — accessed 2026-05-25
3. [Memory: `feedback_msal_scope_must_target_backend_audience.md`](file:///Users/pablomarin/.claude/projects/-Users-pablomarin-Code-msai-v2/memory/feedback_msal_scope_must_target_backend_audience.md) — May 20 outage analysis

**Design impact:** The current `frontend/src/lib/msal-config.ts:42-49` already matches the canonical shape. Gate 1's allow-list logic should mirror it:

- Allow standard OIDC: `openid`, `profile`, `email`, `offline_access` (the latter for refresh-token requests we don't currently use).
- Allow exactly one resource scope matching `^api://<client-id>/[\w.-]+$` where `<client-id>` is resolved from `identity-contract.json`.
- Reject everything else, especially anything containing `graph.microsoft.com`, `User.Read`, `.default` (last is judgment-call — design phase can decide whether to lint against `.default` since it's the PR #74 bug).

**Test implication:** Gate 1's fixture suite should include a test case for `["api://abc/.default"]` and decide (in design) whether to fail or warn. Recommend FAIL given the PR #74 history.

---

## Not Researched (with justification)

- **`@nivo/*`, `recharts`, `lightweight-charts`, `tanstack/react-query`, etc.** — Frontend chart and data libraries. None touched by the auth gates.
- **`databento`, `nautilus_trader`, `ib_async`** — Trading/data libraries. Not in the auth chain.
- **`structlog`, `arq`, `redis`** — Logging/queue libraries. Not in the auth chain.
- **`testcontainers`** — Integration test infra. Gate 2 does not need real Postgres/Redis.
- **`fakeredis`** — Same as above.
- **`@hookform/resolvers`, `react-hook-form`, `zod`** — Form libraries. Not relevant to auth config files.
- **WebSocket auth flow** (`/api/v1/live/stream/{deployment_id}`) — uses `validate_token_or_api_key` in `auth.py:78`, which calls into the same `EntraIDValidator.validate_token`. Already covered transitively by Gate 2's contract test on the middleware. Separate UC for the WebSocket auth handshake is out of scope of THIS feature.

---

## Open Risks

1. **PyJWT does NOT enforce `scp` claim content or presence today.** The backend `validate_token` at `auth.py:48-55` only requires `["exp", "iss", "aud", "sub"]`. Negative cases 3 (missing `scp`), 4 (`scp=User.Read`), and 8 (app-only `roles` no `scp`) in PRD US-002 will NOT fail unless we extend the validator. **Risk:** Gate 2 as specified requires a code change to `auth.py`. The PRD's "No Bugs Left Behind" + "feature changes BEYOND what's needed to enable the gates" non-goal are in tension here. **Mitigation:** treat the validator extension as scope-creep-justified-by-the-gate, OR re-scope Gate 2 cases to drop these three negatives. Design phase must decide explicitly.

2. **Template-literal scope resolution in Gate 1.** The current `msal-config.ts:47` uses `` `api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID || ""}/access_as_user` ``. A custom ESLint rule cannot statically evaluate `process.env` at lint time. **Risk:** the gate either (a) fails closed (treat template literals as opaque → flag every dynamic scope as unresolvable), or (b) requires the template to follow a known-safe shape (`` `api://${EXPRESSION}/CONST_SCOPE_NAME` ``). Approach (b) is cheaper and matches our actual usage but needs the design to specify the safe-template pattern explicitly.

3. **`identity-contract.json` is a brand-new convention.** No precedent in OSS to crib from. **Risk:** Bus factor — if Pablo (and Claude) forget the file's purpose in 12 months, drift creeps back in. **Mitigation:** sibling `identity-contract.README.md` with the rationale, plus Gate 3 failure messages that explicitly explain "this PR drifts from identity-contract.json — the canonical source of truth for tenant/client IDs."

4. **MSAL v5 minor-version drift.** Our `^5.3.0` pin floats to whatever's latest. Pre-release v5.0.0 had a different `protectedResourceMap` shape; current v5.10.1 has it where v4 left it. **Risk:** future v5.x minor adds a NEW interface carrying `scopes` that our Gate 1 doesn't know about. **Mitigation:** import-graph-driven discovery (not hardcoded interface list) — any object literal passed to a method named `acquireToken*` / `loginRedirect` / `loginPopup` / `ssoSilent` is inspected. New methods that match this naming pattern get covered automatically.

5. **PyJWT 2.13.0's tightened `iss` partial-matching prevention.** Version 2.10.1 added "Prevent partial matching of the iss claim." Tests that mint an `iss` that's a substring (not exact match) of the expected issuer will now fail where they passed in 2.9. **Risk:** None in our codebase — we use exact-match string for `iss`. But if a future contributor copies the test fixtures and modifies `iss`, the failure message will be opaque ("InvalidIssuerError") rather than "partial-matching prevented." Document in Gate 2 fixture comments.

6. **CI billable-minutes if all three gates run on every PR.** Three parallel jobs × ~1–2 minutes wall-clock each (assuming caches hit) = ~3–6 runner-minutes per PR. **Risk:** trivial cost; flagged for completeness because PRD §5 mentions "≤ 5 seconds" — that's per-gate wall-clock, achievable. Total billable-minutes are 3× per PR. Not a blocker but worth noting.

7. **The autouse `_override_auth` fixture in `backend/tests/conftest.py:49`.** Bypasses `get_current_user` for every test. Gate 2's contract test in `tests/integration/test_auth_contract.py` MUST disable this override locally. **Risk:** if a future contributor adds the new test file but inherits the autouse, Gate 2's negative cases will silently pass (because the autouse short-circuits the real validator). **Mitigation:** add a module-scoped fixture in the new test file that does `app.dependency_overrides.pop(get_current_user, None)` and verify with a "this should 401" smoke test as the first test in the file.

8. **The `validate_token_or_api_key` helper accepts API keys with no expiry.** Out of scope of this feature, but flagged because Gate 2 might be misread as covering the API key path too. **PRD §2 explicitly excludes** API-key hardening. Just ensure the failure messages from Gate 2 say "JWT validation regression" and not the bare "auth validation regression" — disambiguates which auth path is being tested.

9. **`PyJWKClient` constructs eagerly with the JWKS URL.** Our test code instantiates `EntraIDValidator(TENANT_ID, CLIENT_ID)` which creates a real `PyJWKClient` pointing at `login.microsoftonline.com`. The constructor does NOT fetch — but the test's autouse network-block fixture must allow construction (no socket call) while still blocking subsequent `urlopen` calls. **Mitigation:** monkeypatch `urllib.request.urlopen` to raise; the `PyJWKClient` constructor does not call it; the test's `_jwks_client` swap happens before any decode. Verified by reading PyJWT 2.13.0 source.

---

## Summary of design-changing findings

| #   | Finding                                                                                                                                           | Design impact                                                                                     |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| 1   | PyJWT does NOT enforce `scp` content/presence today — `auth.py:48-55` only requires `["exp","iss","aud","sub"]`                                   | Backend `validate_token` MUST be extended for Gate 2 cases 3/4/8 to fail. In-scope.               |
| 2   | Custom ESLint rule via `@typescript-eslint/utils` is the 2026 canonical pattern; ts-morph is a viable fallback                                    | Pick Option A. Test the rule itself with `RuleTester`.                                            |
| 3   | MSAL v5 scope-array shape is UNCHANGED from v4; current `msal-config.ts` matches canonical Microsoft pattern                                      | Gate 1 allow-list mirrors canonical shape: 4 OIDC scopes + one `api://<client-id>/<name>` scope.  |
| 4   | `identity-contract.json` is a NEW convention; no industry precedent                                                                               | Document rationale in sibling README; runtime stays on env vars; lint-time-only source of truth.  |
| 5   | Existing autouse `_override_auth` fixture in conftest.py will silently bypass Gate 2 if not handled                                               | Gate 2 module locally pops the override; smoke-test asserts 401 returns when override is cleared. |
| 6   | PyJWT exception subclasses (`InvalidAudienceError` etc.) map cleanly to PRD US-002's 9 negative cases — but cases 3/4/8 need custom backend logic | Use `pytest.raises` with SPECIFIC subclass per case; document custom-check cases.                 |
| 7   | Three parallel GH Actions jobs is the right shape; each <5s wall-clock; total ~6 billable-minutes / PR                                            | Workflow file = three sibling jobs, all required for branch protection.                           |
| 8   | Template-literal scope resolution in Gate 1 is genuinely unstaticable from inside ESLint                                                          | Gate 1 must either fail-closed on template literals OR require a known-safe template shape.       |

---

**End of brief.**
