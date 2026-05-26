import { RuleTester } from "@typescript-eslint/rule-tester";
// node:test exports `after`, NOT `afterAll`. Assign to RuleTester.afterAll
// which the lib expects. Confirmed via: node -e
// "import('node:test').then(m => console.log('afterAll' in m))" → false.
import { after, describe, it } from "node:test";
import { msalScopesMustBeApiOrOidc } from "../msal-scopes.mjs";

RuleTester.afterAll = after;
RuleTester.it = it;
RuleTester.itOnly = it;
RuleTester.describe = describe;

const ruleTester = new RuleTester();

ruleTester.run(
  "msal-scopes-must-be-api-or-oidc",
  msalScopesMustBeApiOrOidc,
  {
    valid: [
      { code: `const r = { scopes: ["openid", "profile", "email"] }` },
      { code: `const r = { scopes: ["openid", "offline_access"] }` },
      {
        // Canonical SPA shape — matches current msal-config.ts:43-48.
        code:
          "const r = { scopes: [`api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID}/access_as_user`] }",
      },
      {
        // With `|| ""` fallback (the actual shape in msal-config.ts:47).
        code:
          'const r = { scopes: [`api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID || ""}/access_as_user`] }',
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
        // .default — PR #74 anti-pattern.
        code:
          "const r = { scopes: [`api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID}/.default`] }",
        errors: [{ messageId: "wrong_scope_name" }],
      },
      {
        code: `instance.loginRedirect({ scopes: ["User.Read", "openid"] })`,
        errors: [{ messageId: "forbidden" }],
      },
      {
        // Frontend env-var prefix gate — process.env.AZURE_CLIENT_ID is a
        // BACKEND env var Next.js won't expose to the browser. Rejected
        // as unresolvable_template (would resolve to undefined at runtime).
        code:
          "const r = { scopes: [`api://${process.env.AZURE_CLIENT_ID}/access_as_user`] }",
        errors: [{ messageId: "unresolvable_template" }],
      },
      {
        // Mixed-resource — Graph scope alongside OIDC. Stable across
        // contract value swaps (no hardcoded API client GUID).
        code: `const r = { scopes: ["openid", "User.Read"] }`,
        errors: [{ messageId: "forbidden" }],
      },
      {
        // Codex P1 from code-review iter 1: identifier reference for
        // the scopes array. Without the broadened selector, this would
        // silently bypass the rule entirely — a future contributor
        // could ship `const graphScopes = ["User.Read"]` and the gate
        // wouldn't fire.
        code:
          'const graphScopes = ["User.Read"]; ' +
          "instance.loginRedirect({ scopes: graphScopes })",
        errors: [{ messageId: "unresolvable_template" }],
      },
      {
        // Spread expression inside the scopes array.
        code:
          'const extra = ["User.Read"]; ' +
          "instance.loginRedirect({ scopes: [...extra] })",
        errors: [{ messageId: "unresolvable_template" }],
      },
      {
        // Function-call expression as the scopes value.
        code: "instance.loginRedirect({ scopes: getScopes() })",
        errors: [{ messageId: "unresolvable_template" }],
      },
      {
        // Codex P2 from code-review iter 2: quoted key bypass.
        // { "scopes": [...] } parses with key.type=Literal not Identifier,
        // so the original selector `Property[key.name="scopes"]` missed it.
        code: `const r = { "scopes": ["User.Read"] }`,
        errors: [{ messageId: "forbidden" }],
      },
      {
        // Codex P2 from code-review iter 4: non-empty fallback bypass.
        // `process.env.NAME || "stale-client-id"` would silently build a
        // wrong scope when the env var is absent. Only the empty-string
        // fallback is allowed.
        code:
          "const r = { scopes: " +
          "[`api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID || \"stale-id\"}/access_as_user`] }",
        errors: [{ messageId: "unresolvable_template" }],
      },
    ],
  },
);
