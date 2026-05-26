import { msalScopesMustBeApiOrOidc } from "./msal-scopes.mjs";

export const rules = {
  "msal-scopes-must-be-api-or-oidc": msalScopesMustBeApiOrOidc,
};

export default { rules };
