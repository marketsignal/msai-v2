import { getApiKeyCredential, isApiKeyAuthMode } from "@/lib/auth-mode";

const API_ROOT = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function apiFetch<T>(
  path: string,
  token: string | null,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (isApiKeyAuthMode()) {
    const apiKey = getApiKeyCredential();
    if (apiKey) {
      headers.set("X-API-Key", apiKey);
    }
  } else if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers,
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }

  return (await response.json()) as T;
}
