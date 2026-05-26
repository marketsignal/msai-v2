import { type Configuration, LogLevel } from "@azure/msal-browser";

export const msalConfig: Configuration = {
  auth: {
    clientId: process.env.NEXT_PUBLIC_AZURE_CLIENT_ID || "",
    authority: `https://login.microsoftonline.com/${process.env.NEXT_PUBLIC_AZURE_TENANT_ID || "common"}`,
    redirectUri: typeof window !== "undefined" ? window.location.origin : "",
  },
  cache: {
    cacheLocation: "sessionStorage",
  },
  system: {
    loggerOptions: {
      logLevel: LogLevel.Warning,
    },
  },
};

// Request an access token for the MSAI backend audience, not Microsoft Graph.
// The single Entra ID app registration `NEXT_PUBLIC_AZURE_CLIENT_ID` is shared
// by this SPA and the FastAPI backend (PyJWT validates against the same client
// id). The explicit `access_as_user` scope (defined under "Expose an API" in
// the portal) issues an access token with aud=<client-id> that PyJWT decodes.
//
// Why not `User.Read`: that returns a Microsoft Graph access token (aud=
// 00000003-0000-0000-c000-000000000000), which is opaque to third parties —
// PyJWT can't decode it → "Invalid token: Signature verification failed".
//
// Why not `.default`: Microsoft's docs (learn.microsoft.com/.../scopes-oidc)
// note that a SPA calling its own API with `.default` can return an ID token
// in place of an access token — "new clients shouldn't use that setup."
// `.default` is intended for static/admin-consent flows (OBO, client-creds),
// not interactive SPA→own-API. PR #74 shipped with `.default` and surfaced
// AADSTS500011 (`invalid_resource`) because the Application ID URI wasn't
// configured; the portal config now exists AND we use the explicit scope.
//
// `openid` / `profile` / `email` are OIDC reserved scopes governing ID-token
// claims (name + email shown in the user header). MSAL.js strips them before
// issuing the resource access-token request via acquireTokenSilent, so listing
// them alongside the api:// scope works for both loginRedirect (claims) and
// the silent token fetch (resource token).
// DEMO REGRESSION — will be reverted in the next commit. The auth-regression
// gate (msai/msal-scopes-must-be-api-or-oidc) MUST flag "User.Read" here.
// This is the negative-control proof for PRD US-004.
export const loginRequest = {
  scopes: [
    "User.Read",
    "openid",
    "profile",
    "email",
    `api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID || ""}/access_as_user`,
  ],
};
