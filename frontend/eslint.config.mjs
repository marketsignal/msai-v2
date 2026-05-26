import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";
import msai from "./eslint-rules/index.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    ignores: [
      "node_modules/**",
      ".next/**",
      "out/**",
      "build/**",
      "next-env.d.ts",
      "eslint-rules/**",
    ],
  },
  // Gate 1 — MSAL scope AST lint (see docs/plans/2026-05-25-ui-e2e-auth-setup.md).
  // Catches the May 20 outage class (User.Read / Graph scopes / .default /
  // mixed-resource / non-prefixed env-ref drift) at PR time.
  {
    files: ["src/**/*.ts", "src/**/*.tsx"],
    plugins: { msai },
    rules: {
      "msai/msal-scopes-must-be-api-or-oidc": "error",
    },
  },
];

export default eslintConfig;
