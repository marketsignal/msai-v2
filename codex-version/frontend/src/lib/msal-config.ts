export const msalConfig = {
  auth: {
    clientId: process.env.NEXT_PUBLIC_ENTRA_CLIENT_ID ?? "dev-client-id",
    authority: `https://login.microsoftonline.com/${process.env.NEXT_PUBLIC_ENTRA_TENANT_ID ?? "dev-tenant-id"}`,
    redirectUri: process.env.NEXT_PUBLIC_ENTRA_REDIRECT_URI ?? "http://localhost:3000/login",
  },
  cache: {
    cacheLocation: "localStorage" as const,
    storeAuthStateInCookie: false,
  },
};

const clientId = process.env.NEXT_PUBLIC_ENTRA_CLIENT_ID ?? "dev-client-id";
const defaultApiScope = `api://${clientId}/access_as_user`;

export const loginRequest = {
  scopes: [
    process.env.NEXT_PUBLIC_ENTRA_API_SCOPE ?? defaultApiScope,
    "openid",
    "profile",
    "email",
  ],
};
