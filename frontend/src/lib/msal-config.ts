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
// id). `.default` resolves to the configured scopes under "Expose an API",
// producing an access token with aud=<client-id> that PyJWT decodes.
//
// Why not `User.Read`: that returns a Microsoft Graph access token (aud=
// 00000003-0000-0000-c000-000000000000), which is opaque to third parties —
// PyJWT can't decode it → "Invalid token: Signature verification failed".
//
// `openid` / `profile` / `email` are OIDC reserved scopes governing ID-token
// claims (name + email shown in the user header). MSAL.js strips them before
// issuing the resource access-token request via acquireTokenSilent, so listing
// them alongside the api:// scope works for both loginRedirect (claims) and
// the silent token fetch (resource token).
export const loginRequest = {
  scopes: [
    "openid",
    "profile",
    "email",
    `api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID || ""}/.default`,
  ],
};
