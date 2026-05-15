/**
 * Typed client for `/api/v1/live-portfolios/*` CRUD endpoints.
 *
 * Live portfolios are the control-plane container for revisions; revisions
 * become immutable once frozen via `snapshotPortfolio`.
 *
 * Auth follows the pattern in `@/lib/api`: Bearer token takes precedence,
 * otherwise the `NEXT_PUBLIC_MSAI_API_KEY` header is sent automatically.
 */

import { apiGet, apiPost } from "@/lib/api";
import type { LivePortfolio, LivePortfolioRevision } from "@/lib/api";

/** Frozen member row returned by `addPortfolioMember`. */
export interface LivePortfolioMember {
  id: string;
  strategy_id: string;
  config: Record<string, unknown>;
  instruments: string[];
  /** Decimal serialized as a string. */
  weight: string;
  order_index: number;
}

/** POST /api/v1/live-portfolios — create a new (empty) live portfolio. */
export async function createLivePortfolio(
  body: { name: string; description?: string | null },
  token?: string | null,
): Promise<LivePortfolio> {
  return apiPost<LivePortfolio>("/api/v1/live-portfolios", body, token);
}

/** GET /api/v1/live-portfolios — list all live portfolios. */
export async function listLivePortfolios(
  token?: string | null,
): Promise<LivePortfolio[]> {
  return apiGet<LivePortfolio[]>("/api/v1/live-portfolios", token);
}

/** GET /api/v1/live-portfolios/{id} — fetch one live portfolio by id. */
export async function getLivePortfolio(
  portfolioId: string,
  token?: string | null,
): Promise<LivePortfolio> {
  return apiGet<LivePortfolio>(
    `/api/v1/live-portfolios/${encodeURIComponent(portfolioId)}`,
    token,
  );
}

/**
 * POST /api/v1/live-portfolios/{portfolioId}/strategies — append a member
 * (strategy + config + instruments + weight) to the working revision.
 *
 * `weight` accepts either a number or a stringified decimal; the backend
 * serializes the response weight back as a string.
 */
export async function addPortfolioMember(
  portfolioId: string,
  body: {
    strategy_id: string;
    config: Record<string, unknown>;
    instruments: string[];
    weight: number | string;
  },
  token?: string | null,
): Promise<LivePortfolioMember> {
  return apiPost<LivePortfolioMember>(
    `/api/v1/live-portfolios/${encodeURIComponent(portfolioId)}/strategies`,
    body,
    token,
  );
}

/**
 * GET /api/v1/live-portfolios/{portfolioId}/members — list the current
 * DRAFT revision's members (empty after snapshot until a new strategy
 * is added). Used by the compose flow to surface any persisted members
 * when reopening a portfolio (Codex iter-6 P2 fix).
 */
export async function listDraftMembers(
  portfolioId: string,
  token?: string | null,
): Promise<LivePortfolioMember[]> {
  return apiGet<LivePortfolioMember[]>(
    `/api/v1/live-portfolios/${encodeURIComponent(portfolioId)}/members`,
    token,
  );
}

/**
 * POST /api/v1/live-portfolios/{portfolioId}/snapshot — freeze the working
 * revision and return the new immutable `LivePortfolioRevision`.
 */
export async function snapshotPortfolio(
  portfolioId: string,
  token?: string | null,
): Promise<LivePortfolioRevision> {
  return apiPost<LivePortfolioRevision>(
    `/api/v1/live-portfolios/${encodeURIComponent(portfolioId)}/snapshot`,
    {},
    token,
  );
}
