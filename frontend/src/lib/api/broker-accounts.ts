/**
 * Typed client for `/api/v1/broker-accounts/*` CRUD endpoints.
 *
 * Broker accounts are the control-plane entity for the multi-account IB
 * fleet. TWS credentials are written server-side to the secrets backend on
 * create/rotate and are NEVER returned — responses carry only metadata
 * references (`credentials_secret_ref` / `credentials_secret_version`) and
 * audit columns. The request types (`BrokerAccountCreate`,
 * `BrokerAccountRotateCredentials`) carry the credentials inbound only.
 *
 * Auth follows the pattern in `@/lib/api`: Bearer token takes precedence,
 * otherwise the `NEXT_PUBLIC_MSAI_API_KEY` header is sent automatically.
 */

import { apiFetch, apiGet, apiPost, ApiError } from "@/lib/api";

/**
 * Broker-account metadata — mirror of backend `BrokerAccountResponse`.
 *
 * METADATA ONLY: deliberately has no `tws_userid` / `tws_password`. The
 * secret never leaves the secrets backend; the UI surfaces only the
 * reference + audit columns below.
 */
export interface BrokerAccount {
  id: string;
  ib_account_id: string;
  ib_login_key: string;
  label: string | null;
  status: string;
  gateway_slot: string;
  trading_mode: string;
  credentials_backend: string;
  credentials_secret_ref: string;
  credentials_secret_version: string | null;
  credentials_updated_at: string | null;
  credentials_updated_by: string | null;
  credentials_last_accessed: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * POST body for registering a broker account — mirror of backend
 * `BrokerAccountCreateRequest`. Carries the TWS credentials inbound so the
 * backend can write them to the secrets backend; they are never echoed back.
 */
export interface BrokerAccountCreate {
  ib_account_id: string;
  ib_login_key: string;
  label?: string | null;
  /** "paper" | "live" (backend defaults to "paper"). */
  trading_mode: string;
  /** Omit to auto-allocate a free gateway slot. */
  gateway_slot?: string | null;
  tws_userid: string;
  tws_password: string;
}

/**
 * PATCH body for a broker account — mirror of backend
 * `BrokerAccountUpdateRequest`. `ib_account_id` is immutable (absent here).
 */
export interface BrokerAccountUpdate {
  label?: string | null;
  /** "paper" | "live". */
  trading_mode?: string | null;
}

/**
 * POST body for rotating credentials — mirror of backend
 * `BrokerAccountRotateCredentialsRequest`.
 */
export interface BrokerAccountRotateCredentials {
  tws_userid: string;
  tws_password: string;
}

/** GET /api/v1/broker-accounts — list broker accounts (bare array). */
export async function listBrokerAccounts(
  token?: string | null,
): Promise<BrokerAccount[]> {
  return apiGet<BrokerAccount[]>("/api/v1/broker-accounts", token);
}

/** GET /api/v1/broker-accounts/{id} — fetch one broker account by id. */
export async function getBrokerAccount(
  id: string,
  token?: string | null,
): Promise<BrokerAccount> {
  return apiGet<BrokerAccount>(
    `/api/v1/broker-accounts/${encodeURIComponent(id)}`,
    token,
  );
}

/**
 * POST /api/v1/broker-accounts — register a broker account. The secret is
 * written server-side first; the response is metadata only.
 */
export async function createBrokerAccount(
  body: BrokerAccountCreate,
  token?: string | null,
): Promise<BrokerAccount> {
  return apiPost<BrokerAccount>("/api/v1/broker-accounts", body, token);
}

/**
 * PATCH /api/v1/broker-accounts/{id} — update label / trading_mode. Uses
 * `apiFetch` directly because `@/lib/api` exposes no `apiPatch` helper.
 */
export async function updateBrokerAccount(
  id: string,
  body: BrokerAccountUpdate,
  token?: string | null,
): Promise<BrokerAccount> {
  const path = `/api/v1/broker-accounts/${encodeURIComponent(id)}`;
  const res = await apiFetch(
    path,
    { method: "PATCH", body: JSON.stringify(body) },
    token,
  );
  if (!res.ok) {
    let errBody: unknown = null;
    try {
      errBody = await res.json();
    } catch {
      // ignore — body may be empty / non-JSON
    }
    throw new ApiError(
      `PATCH ${path} failed: ${res.status}`,
      res.status,
      errBody,
    );
  }
  return (await res.json()) as BrokerAccount;
}

/**
 * POST /api/v1/broker-accounts/{id}/rotate-credentials — replace the stored
 * TWS credentials; the version advances and the response reflects the new
 * `credentials_secret_version`.
 */
export async function rotateBrokerAccountCredentials(
  id: string,
  body: BrokerAccountRotateCredentials,
  token?: string | null,
): Promise<BrokerAccount> {
  return apiPost<BrokerAccount>(
    `/api/v1/broker-accounts/${encodeURIComponent(id)}/rotate-credentials`,
    body,
    token,
  );
}

/**
 * POST /api/v1/broker-accounts/{id}/archive — soft-delete: frees the gateway
 * slot, deletes the stored secret, and returns the updated (archived)
 * resource.
 */
export async function archiveBrokerAccount(
  id: string,
  token?: string | null,
): Promise<BrokerAccount> {
  return apiPost<BrokerAccount>(
    `/api/v1/broker-accounts/${encodeURIComponent(id)}/archive`,
    {},
    token,
  );
}
