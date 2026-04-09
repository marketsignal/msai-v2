"use client";

export type AuthMode = "entra" | "api-key";

const API_KEY_AUTH_MODE = "api-key";

export function getAuthMode(): AuthMode {
  return process.env.NEXT_PUBLIC_AUTH_MODE === API_KEY_AUTH_MODE
    ? API_KEY_AUTH_MODE
    : "entra";
}

export function isApiKeyAuthMode(): boolean {
  return getAuthMode() === API_KEY_AUTH_MODE;
}

export function getApiKeyCredential(): string | null {
  const value = process.env.NEXT_PUBLIC_E2E_API_KEY?.trim();
  return value ? value : null;
}

export function isLiveStreamEnabled(): boolean {
  return process.env.NEXT_PUBLIC_LIVE_STREAM_ENABLED !== "false";
}
