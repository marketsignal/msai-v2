// @ts-check
/**
 * MSAL scope AST lint rule.
 *
 * Enforces that every `scopes: [...]` literal in MSAL.js call sites or
 * exported request objects contains ONLY:
 *   - standard OIDC scopes: openid, profile, email, offline_access
 *   - exactly one resource scope matching `api://<client-id>/<scope-name>`
 *     where <client-id> and <scope-name> come from identity-contract.json.
 *
 * Rejects: `User.Read`, `graph.microsoft.com/*`, `.default`, mixed-resource
 * arrays, fallback/default scopes that aren't the API scope.
 *
 * The May 20 outage class — `scopes: ["User.Read", ...]` — is caught at
 * the literal level here, BEFORE the SPA build ever ships.
 */

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
 * @property {{ client_id: string[]; tenant_id: string[] }} env_var_names
 * @property {string} [frontend_env_prefix]
 */

/**
 * Find identity-contract.json by walking up from the rule's module
 * location (NOT context.cwd — brittle when lint runs from an editor at
 * the repo root vs CI in `frontend/`).
 *
 * @returns {IdentityContract}
 */
function loadContract() {
  const __filename = fileURLToPath(import.meta.url);
  let dir = dirname(__filename);
  for (let i = 0; i < 10; i++) {
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
        "MSAL scope literals must be either standard OIDC " +
        "(openid/profile/email/offline_access) or " +
        "api://<client-id>/<scope-name> matching identity-contract.json. " +
        "Forbids User.Read, graph.microsoft.com, .default — the May 20 outage class.",
    },
    schema: [],
    messages: {
      forbidden:
        'Forbidden MSAL scope literal "{{value}}" — expected ' +
        '"api://<client-id>/{{expectedScope}}" or one of openid/profile/email/offline_access.',
      wrong_scope_name:
        'MSAL scope name "{{actual}}" does not match ' +
        'identity-contract.json#scope_name ("{{expected}}").',
      unresolvable_template:
        "Cannot statically resolve MSAL scope template literal. " +
        "Use the canonical shape `api://${NEXT_PUBLIC_AZURE_CLIENT_ID}/{{expectedScope}}` " +
        "where the scope name is a literal and the interpolation is a " +
        "NEXT_PUBLIC_ env-var reference.",
    },
  },
  defaultOptions: [],
  create(context) {
    const contract = loadContract();
    const apiScopePattern = new RegExp(
      `^api://${contract.client_id}/${contract.scope_name}$`,
    );

    // Canonical env-var references trusted as resolving to the contract's
    // client_id WHEN USED IN FRONTEND CODE. Filtered to vars beginning
    // with frontend_env_prefix (typically NEXT_PUBLIC_) because Next.js
    // only exposes prefixed env vars to the browser bundle — a non-prefixed
    // backend env var would silently resolve to undefined at runtime.
    const frontendPrefix = contract.frontend_env_prefix ?? "NEXT_PUBLIC_";
    const allowedClientIdEnvRefs = new Set(
      contract.env_var_names.client_id
        .filter((name) => name.startsWith(frontendPrefix))
        .map((name) => `process.env.${name}`),
    );

    /**
     * @param {import("@typescript-eslint/utils").TSESTree.Literal | import("@typescript-eslint/utils").TSESTree.TemplateLiteral} node
     * @param {string} raw
     */
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

      if (!raw.startsWith("api://")) {
        context.report({
          node,
          messageId: "forbidden",
          data: { value: raw, expectedScope: contract.scope_name },
        });
        return;
      }

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
     * Stringify an AST expression for comparison against the allowed
     * env-var passthrough list. Returns null if the expression isn't a
     * recognised shape.
     *
     * Supports:
     *   - `process.env.NAME`
     *   - `process.env.NAME || ""` (empty-string fallback only; a
     *     non-empty fallback is REJECTED to prevent stale-client-id drift —
     *     Codex P2 from code-review iter 4)
     *
     * @param {import("@typescript-eslint/utils").TSESTree.Expression | null} expr
     * @returns {string | null}
     */
    function expressionAsEnvRef(expr) {
      if (!expr) return null;
      if (
        expr.type === "MemberExpression" &&
        expr.object?.type === "MemberExpression" &&
        expr.object.object?.type === "Identifier" &&
        expr.object.object.name === "process" &&
        expr.object.property?.type === "Identifier" &&
        expr.object.property.name === "env" &&
        expr.property?.type === "Identifier"
      ) {
        return `process.env.${expr.property.name}`;
      }
      if (expr.type === "LogicalExpression" && expr.operator === "||") {
        // ONLY accept `process.env.NAME || ""` (empty-string fallback).
        // A non-empty fallback like `process.env.NAME || "stale-id"` would
        // build a wrong resource scope when the env var is absent — that's
        // exactly the kind of drift Gate 1 must reject.
        const right = /** @type {any} */ (expr.right);
        if (
          right?.type !== "Literal" ||
          typeof right.value !== "string" ||
          right.value !== ""
        ) {
          return null;
        }
        return expressionAsEnvRef(/** @type {any} */ (expr.left));
      }
      return null;
    }

    /**
     * @param {import("@typescript-eslint/utils").TSESTree.TemplateLiteral} node
     */
    function checkTemplateLiteral(node) {
      if (node.expressions.length === 0) {
        checkLiteral(node, node.quasis[0]?.value.raw ?? "");
        return;
      }
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
          data: { expectedScope: contract.scope_name },
        });
        return;
      }
      const envRef = expressionAsEnvRef(node.expressions[0]);
      if (!envRef || !allowedClientIdEnvRefs.has(envRef)) {
        context.report({
          node,
          messageId: "unresolvable_template",
          data: { expectedScope: contract.scope_name },
        });
        return;
      }
      const literalScopeName = parts[1].slice(1);
      if (literalScopeName !== contract.scope_name) {
        context.report({
          node,
          messageId: "wrong_scope_name",
          data: { actual: literalScopeName, expected: contract.scope_name },
        });
      }
    }

    /**
     * @param {import("@typescript-eslint/utils").TSESTree.ArrayExpression} node
     */
    function visitScopesArray(node) {
      for (const el of node.elements) {
        if (el === null) continue;
        if (el.type === "Literal" && typeof el.value === "string") {
          checkLiteral(el, el.value);
        } else if (el.type === "TemplateLiteral") {
          checkTemplateLiteral(el);
        } else if (el.type === "SpreadElement") {
          // `scopes: [...someVar]` — can't statically resolve.
          // Fail closed per US-001 acceptance.
          context.report({
            node: el,
            messageId: "unresolvable_template",
            data: { expectedScope: contract.scope_name },
          });
        } else {
          // Identifier / member-expression / call etc. Same fail-closed.
          context.report({
            node: el,
            messageId: "unresolvable_template",
            data: { expectedScope: contract.scope_name },
          });
        }
      }
    }

    /**
     * Return true if a Property key node names "scopes" — handling both
     * identifier keys (`scopes:`) AND quoted string keys (`"scopes":`).
     * Codex P2 from code-review iter 2.
     *
     * @param {any} keyNode
     */
    function isScopesKey(keyNode) {
      if (!keyNode) return false;
      if (keyNode.type === "Identifier" && keyNode.name === "scopes") return true;
      if (
        keyNode.type === "Literal" &&
        typeof keyNode.value === "string" &&
        keyNode.value === "scopes"
      ) {
        return true;
      }
      return false;
    }

    return {
      // Match EVERY Property; dispatch based on whether it's named
      // "scopes" (identifier OR quoted-string key). Identifier / spread /
      // function-call values fail closed via the unresolvable_template
      // message. Codex iter 1 P1 + iter 2 P2.
      Property(node) {
        if (!isScopesKey(/** @type {any} */ (node).key)) return;
        const value = /** @type {any} */ (node).value;
        if (!value) return;
        if (value.type === "ArrayExpression") {
          visitScopesArray(value);
          return;
        }
        // Identifier, MemberExpression, CallExpression, ConditionalExpression,
        // LogicalExpression, TemplateLiteral-as-value, etc. — cannot
        // statically prove the scope array's contents. Fail closed.
        context.report({
          node: value,
          messageId: "unresolvable_template",
          data: { expectedScope: contract.scope_name },
        });
      },
    };
  },
});
