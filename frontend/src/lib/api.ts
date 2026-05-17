/**
 * API client for MSAI v2 backend.
 *
 * Supports two auth modes:
 * 1. Bearer token (Entra ID JWT) — for browser SSO
 * 2. X-API-Key header — for dev/local, passed via NEXT_PUBLIC_MSAI_API_KEY
 *
 * If a token is provided via the `token` argument it takes precedence.
 * Otherwise, if NEXT_PUBLIC_MSAI_API_KEY is set, the API key is used.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8800";
const API_KEY = process.env.NEXT_PUBLIC_MSAI_API_KEY || "";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public body: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * Format an error for user-facing toast / inline alert. Prefers the
 * backend's ``detail`` payload (FastAPI HTTPException) so the user sees
 * the real reason ("config_schema mismatch on field X") instead of a
 * generic "Save failed (422)". Falls back to the HTTP status, then the
 * raw message. Per Codex iter-1 / silent-failure-hunter F4.
 */
export function describeApiError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (
      err.body &&
      typeof err.body === "object" &&
      "detail" in err.body &&
      err.body.detail !== null
    ) {
      const detail = (err.body as { detail: unknown }).detail;
      if (typeof detail === "string") return detail;
      try {
        return JSON.stringify(detail);
      } catch {
        // ignore — fall through to the status-suffixed fallback
      }
    }
    return `${fallback} (${err.status})`;
  }
  if (err instanceof Error) return err.message || fallback;
  return fallback;
}

export async function apiFetch(
  path: string,
  options: RequestInit = {},
  token?: string | null,
): Promise<Response> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  } else if (API_KEY) {
    headers["X-API-Key"] = API_KEY;
  }
  return fetch(`${API_BASE}${path}`, { ...options, headers });
}

/**
 * Throw an ``ApiError`` for a non-OK ``Response``. Best-effort JSON-parses
 * the body so consumers can pull ``detail`` via ``describeApiError``. Shared
 * by every fetcher in this module so the "parse body, throw" pattern lives
 * in one place.
 */
async function throwApiError(
  method: string,
  path: string,
  res: Response,
): Promise<never> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    // ignore — body may be empty / non-JSON
  }
  throw new ApiError(
    `${method} ${path} failed: ${res.status}`,
    res.status,
    body,
  );
}

/** Fetch JSON from the API, throwing ApiError on non-2xx. */
export async function apiGet<T>(
  path: string,
  token?: string | null,
): Promise<T> {
  const res = await apiFetch(path, { method: "GET" }, token);
  if (!res.ok) await throwApiError("GET", path, res);
  return (await res.json()) as T;
}

/** POST JSON to the API, throwing ApiError on non-2xx. */
export async function apiPost<T>(
  path: string,
  body: unknown,
  token?: string | null,
): Promise<T> {
  const res = await apiFetch(
    path,
    { method: "POST", body: JSON.stringify(body) },
    token,
  );
  if (!res.ok) await throwApiError("POST", path, res);
  return (await res.json()) as T;
}

// =====================================================================
// API response types — mirror of backend Pydantic schemas
// =====================================================================

export interface SymbolsResponse {
  symbols: Record<string, string[]>;
}

export interface BarPoint {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface BarsResponse {
  symbol: string;
  interval: string;
  bars: BarPoint[];
  count: number;
}

export type ConfigSchemaStatus =
  | "ready"
  | "unsupported"
  | "extraction_failed"
  | "no_config_class";

export interface StrategyResponse {
  id: string;
  name: string;
  description: string;
  strategy_class: string;
  code_hash: string;
  file_path: string;
  config_schema: Record<string, unknown> | null;
  default_config: Record<string, unknown> | null;
  /**
   * Emitted by backend ``build_user_schema()`` — one of:
   * - ``"ready"`` — ``config_schema`` + ``default_config`` are both non-null.
   * - ``"unsupported"`` — the strategy's Config uses a type the schema_hook doesn't cover.
   * - ``"extraction_failed"`` — msgspec.json.schema raised an unexpected exception.
   * - ``"no_config_class"`` — the strategy has no matching ``*Config`` class.
   *
   * Frontend auto-form (``SchemaForm``) activates only on ``"ready"``.
   */
  config_schema_status: ConfigSchemaStatus;
  created_at: string;
}

export interface StrategyListResponse {
  items: StrategyResponse[];
  total: number;
}

export type RemediationKind =
  | "ingest_data"
  | "contact_support"
  | "retry"
  | "none";

export interface Remediation {
  kind: RemediationKind;
  symbols?: string[] | null;
  asset_class?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  auto_available: boolean;
}

export interface ErrorEnvelope {
  code: string;
  message: string;
  suggested_action?: string | null;
  remediation?: Remediation | null;
}

export interface BacktestHistoryItem {
  id: string;
  strategy_id: string;
  status: "pending" | "running" | "completed" | "failed";
  start_date: string;
  end_date: string;
  created_at: string;
  error_code?: string | null;
  error_public_message?: string | null;
  phase?: "awaiting_data" | null;
  progress_message?: string | null;
}

export interface BacktestHistoryResponse {
  items: BacktestHistoryItem[];
  total: number;
}

export interface BacktestStatusResponse {
  id: string;
  status: "pending" | "running" | "completed" | "failed";
  progress: number;
  // optional (not just nullable) because the backend uses
  // ``response_model_exclude_none=True`` — pending/running rows have these
  // absent, not null. Same applies to ``error`` below.
  started_at?: string | null;
  completed_at?: string | null;
  error?: ErrorEnvelope | null;
  phase?: "awaiting_data" | null;
  progress_message?: string | null;
}

export interface BacktestMetrics {
  sharpe_ratio: number;
  sortino_ratio: number;
  max_drawdown: number;
  total_return: number;
  win_rate: number;
  num_trades: number;
  final_equity?: number;
  initial_cash?: number;
}

// ---------------------------------------------------------------------------
// Canonical normalized series payload — mirror of backend SeriesPayload
// ---------------------------------------------------------------------------

export type SeriesStatus = "ready" | "not_materialized" | "failed";

export interface SeriesDailyPoint {
  /** ISO YYYY-MM-DD. */
  date: string;
  equity: number;
  /** Non-positive by construction. */
  drawdown: number;
  daily_return: number;
}

export interface SeriesMonthlyReturn {
  /** YYYY-MM (zero-padded). */
  month: string;
  pct: number;
}

export interface SeriesPayload {
  daily: SeriesDailyPoint[];
  monthly_returns: SeriesMonthlyReturn[];
}

export interface BacktestResultsResponse {
  id: string;
  metrics: BacktestMetrics | null;
  trade_count: number;
  /** Canonical normalized series — ``null`` when ``series_status !== "ready"``. */
  series: SeriesPayload | null;
  series_status: SeriesStatus;
  /** ``true`` when the "Full report" iframe tab should be enabled (derived
   * server-side from ``Backtest.report_path is not None``). */
  has_report: boolean;
  // trades removed — use getBacktestTrades() for paginated fills
}

// =====================================================================
// Live trading types — mirror of backend schemas/live.py
// =====================================================================

export interface LiveDeploymentInfo {
  id: string;
  strategy_id: string;
  status: string;
  paper_trading: boolean;
  instruments: string[] | null;
  started_at: string | null;
  stopped_at: string | null;
}

export interface LiveStatusResponse {
  deployments: LiveDeploymentInfo[];
  risk_halted: boolean;
  active_count: number;
}

/** Fetch the current list of live deployments + global state. */
export async function getLiveStatus(
  token?: string | null,
): Promise<LiveStatusResponse> {
  return apiGet<LiveStatusResponse>("/api/v1/live/status", token);
}

// ---------------------------------------------------------------------------
// Account
// ---------------------------------------------------------------------------

export interface AccountSummary {
  net_liquidation: number;
  buying_power: number;
  available_funds: number;
  margin_used: number;
  unrealized_pnl: number;
  realized_pnl: number;
}

export async function getAccountSummary(
  token?: string | null,
): Promise<AccountSummary> {
  return apiGet<AccountSummary>("/api/v1/account/summary", token);
}

// ---------------------------------------------------------------------------
// Live Positions (REST — complement to WebSocket)
// ---------------------------------------------------------------------------

export interface LivePositionItem {
  deployment_id: string;
  instrument_id: string;
  qty: string;
  avg_price: string;
  unrealized_pnl: string;
  realized_pnl: string;
  ts: string;
}

export interface LivePositionsResponse {
  positions: LivePositionItem[];
}

export async function getLivePositions(
  token?: string | null,
): Promise<LivePositionsResponse> {
  return apiGet<LivePositionsResponse>("/api/v1/live/positions", token);
}

// ---------------------------------------------------------------------------
// Live Trades (REST)
// ---------------------------------------------------------------------------

export interface LiveTrade {
  id: string;
  deployment_id: string | null;
  instrument_id: string;
  side: string;
  quantity: string;
  price: string | null;
  order_type: string;
  status: string;
  client_order_id: string;
  timestamp: string;
}

export interface LiveTradesResponse {
  trades: LiveTrade[];
  total: number;
}

export async function getLiveTrades(
  token?: string | null,
): Promise<LiveTradesResponse> {
  return apiGet<LiveTradesResponse>("/api/v1/live/trades", token);
}

// =====================================================================
// Backtest detail types (equity curve, trades)
// =====================================================================

export interface EquityPoint {
  date: string;
  equity: number;
  drawdown: number;
}

/**
 * One individual Nautilus fill from a backtest — matches the backend
 * ``BacktestTradeItem`` Pydantic model. The earlier shape (entry/exit
 * pairs with ``holdingPeriod``) was a UI-only fabrication that never
 * aligned with the backend's per-fill ``Trade`` row; this is the
 * canonical per-fill payload that ``<TradeLog>`` renders directly.
 */
export interface BacktestTradeItem {
  id: string;
  instrument: string;
  side: "BUY" | "SELL";
  quantity: number;
  price: number;
  pnl: number;
  commission: number;
  /** ISO datetime (UTC). */
  executed_at: string;
}

export interface BacktestTradesResponse {
  items: BacktestTradeItem[];
  total: number;
  page: number;
  page_size: number;
}

/** Fetch a paginated slice of fills for a backtest. Backend clamps
 *  ``page_size`` at 500 server-side; the default matches the UI's page size.
 */
export async function getBacktestTrades(
  id: string,
  params: { page: number; page_size?: number },
  token?: string | null,
): Promise<BacktestTradesResponse> {
  const q = new URLSearchParams({
    page: String(params.page),
    page_size: String(params.page_size ?? 100),
  });
  return apiGet<BacktestTradesResponse>(
    `/api/v1/backtests/${encodeURIComponent(id)}/trades?${q.toString()}`,
    token,
  );
}

// ---------------------------------------------------------------------------
// Signed-URL report token
// ---------------------------------------------------------------------------

export interface BacktestReportTokenResponse {
  /** Absolute API path (``/api/v1/...``) carrying an HMAC ``?token=...``
   * query. Origin-qualified against ``NEXT_PUBLIC_API_URL`` by
   * ``<ReportIframe>`` before use as iframe ``src``. */
  signed_url: string;
  /** ISO datetime. Tokens expire after
   * ``settings.report_token_ttl_seconds`` (default 60s). */
  expires_at: string;
}

/** Mint a short-lived HMAC-signed URL for the QuantStats iframe.
 *  Always POST with an empty body (the handler takes no payload). */
export async function getBacktestReportToken(
  id: string,
  token?: string | null,
): Promise<BacktestReportTokenResponse> {
  return apiPost<BacktestReportTokenResponse>(
    `/api/v1/backtests/${encodeURIComponent(id)}/report-token`,
    {},
    token,
  );
}

export interface MonthlyReturn {
  month: string;
  year: number;
  return_pct: number;
}

// =====================================================================
// Market data status types — mirror of backend schemas/market_data.py
// =====================================================================

export interface StorageStatsResponse {
  asset_classes: Record<string, number>;
  total_files: number;
  total_bytes: number;
}

export interface MarketDataStatusResponse {
  status: string;
  storage: StorageStatsResponse;
}

export async function getMarketDataStatus(
  token?: string | null,
): Promise<MarketDataStatusResponse> {
  return apiGet<MarketDataStatusResponse>("/api/v1/market-data/status", token);
}

export interface MarketDataSymbolsResponse {
  symbols: Record<string, string[]>;
}

export async function getMarketDataSymbols(
  token?: string | null,
): Promise<MarketDataSymbolsResponse> {
  return apiGet<MarketDataSymbolsResponse>(
    "/api/v1/market-data/symbols",
    token,
  );
}

// =====================================================================
// Research types — mirror of backend schemas/research.py
// =====================================================================

export interface ResearchJobResponse {
  id: string;
  strategy_id: string;
  job_type: string;
  status: string;
  progress: number;
  progress_message: string | null;
  best_config: Record<string, unknown> | null;
  best_metrics: Record<string, unknown> | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface ResearchJobListResponse {
  items: ResearchJobResponse[];
  total: number;
}

export interface ResearchTrialResponse {
  id: string;
  trial_number: number;
  config: Record<string, unknown>;
  metrics: Record<string, unknown> | null;
  status: string;
  objective_value: number | null;
  backtest_id: string | null;
  created_at: string;
}

export interface ResearchJobDetailResponse extends ResearchJobResponse {
  config: Record<string, unknown>;
  results: Record<string, unknown> | null;
  trials: ResearchTrialResponse[];
}

export interface ResearchPromotionResponse {
  candidate_id: string;
  stage: string;
  message: string;
}

// =====================================================================
// Graduation types — mirror of backend schemas/graduation.py
// =====================================================================

export interface GraduationCandidateResponse {
  id: string;
  strategy_id: string;
  research_job_id: string | null;
  stage: string;
  config: Record<string, unknown>;
  metrics: Record<string, unknown>;
  deployment_id: string | null;
  notes: string | null;
  promoted_by: string | null;
  promoted_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface GraduationCandidateListResponse {
  items: GraduationCandidateResponse[];
  total: number;
}

export interface GraduationTransitionResponse {
  id: number;
  candidate_id: string;
  from_stage: string;
  to_stage: string;
  reason: string | null;
  transitioned_by: string | null;
  created_at: string;
}

export interface GraduationTransitionListResponse {
  items: GraduationTransitionResponse[];
  total: number;
}

// =====================================================================
// Portfolio types — mirror of backend schemas/portfolio.py
// =====================================================================

export interface PortfolioResponse {
  id: string;
  name: string;
  description: string | null;
  objective: string;
  base_capital: number;
  requested_leverage: number;
  benchmark_symbol: string | null;
  account_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface PortfolioListResponse {
  items: PortfolioResponse[];
  total: number;
}

export interface PortfolioRunResponse {
  id: string;
  portfolio_id: string;
  status: string;
  metrics: Record<string, unknown> | null;
  report_path: string | null;
  start_date: string;
  end_date: string;
  created_at: string;
  completed_at: string | null;
}

export interface PortfolioRunListResponse {
  items: PortfolioRunResponse[];
  total: number;
}

// ─── Inventory + symbol-onboarding types (universe-page) ───────────────────

export type AssetClass = "equity" | "futures" | "fx" | "option";
export type InventoryStatus =
  | "ready"
  | "stale"
  | "gapped"
  | "backtest_only"
  | "live_only"
  | "not_registered";

export interface InventoryRow {
  instrument_uid: string;
  symbol: string;
  asset_class: AssetClass;
  provider: string;
  registered: boolean;
  backtest_data_available: boolean | null;
  coverage_status: "full" | "gapped" | "none" | null;
  covered_range: string | null;
  missing_ranges: { start: string; end: string }[];
  is_stale: boolean;
  live_qualified: boolean;
  last_refresh_at: string | null;
  status: InventoryStatus;
}

export interface OnboardSymbolSpec {
  symbol: string;
  asset_class: AssetClass;
  start: string;
  end: string;
}

export interface OnboardRequest {
  watchlist_name: string;
  symbols: OnboardSymbolSpec[];
  request_live_qualification?: boolean;
  cost_ceiling_usd?: string;
}

export type OnboardLifecycle =
  | "pending"
  | "in_progress"
  | "completed"
  | "completed_with_failures"
  | "failed";

export interface OnboardResponse {
  run_id: string;
  watchlist_name: string;
  status: OnboardLifecycle;
}

export interface DryRunResponse {
  watchlist_name: string;
  dry_run: true;
  estimated_cost_usd: string;
  estimate_basis: string;
  estimate_confidence: "high" | "medium" | "low";
  symbol_count: number;
  breakdown: Array<Record<string, unknown>>;
}

export interface OnboardStatusResponse {
  run_id: string;
  watchlist_name: string;
  status: OnboardLifecycle;
  progress: {
    total: number;
    succeeded: number;
    failed: number;
    in_progress: number;
    not_started: number;
  };
  per_symbol: Array<{
    symbol: string;
    asset_class: AssetClass;
    start: string;
    end: string;
    status: "not_started" | "in_progress" | "succeeded" | "failed";
    step: string;
    error: Record<string, unknown> | null;
    next_action: string | null;
  }>;
  estimated_cost_usd: string | null;
  actual_cost_usd: string | null;
}

export async function getInventory(
  token: string | null,
  params: { start?: string; end?: string; asset_class?: AssetClass } = {},
): Promise<InventoryRow[]> {
  const query = new URLSearchParams();
  if (params.start) query.set("start", params.start);
  if (params.end) query.set("end", params.end);
  if (params.asset_class) query.set("asset_class", params.asset_class);
  const qs = query.toString();
  const path = `/api/v1/symbols/inventory${qs ? "?" + qs : ""}`;
  return apiGet<InventoryRow[]>(path, token);
}

export async function postOnboard(
  token: string | null,
  body: OnboardRequest,
): Promise<OnboardResponse> {
  return apiPost<OnboardResponse>("/api/v1/symbols/onboard", body, token);
}

export async function postOnboardDryRun(
  token: string | null,
  body: OnboardRequest,
): Promise<DryRunResponse> {
  return apiPost<DryRunResponse>(
    "/api/v1/symbols/onboard/dry-run",
    body,
    token,
  );
}

export async function getOnboardStatus(
  token: string | null,
  runId: string,
): Promise<OnboardStatusResponse> {
  return apiGet<OnboardStatusResponse>(
    `/api/v1/symbols/onboard/${runId}/status`,
    token,
  );
}

/**
 * DELETE /api/v1/symbols/{symbol}?asset_class=... — soft-deletes inventory row.
 * Backend disambiguates by (symbol, asset_class) since the same ticker can map
 * to multiple instruments across asset classes (Override O-3 + O-10).
 */
export async function deleteSymbol(
  token: string | null,
  args: { symbol: string; asset_class: AssetClass },
): Promise<void> {
  const path =
    `/api/v1/symbols/${encodeURIComponent(args.symbol)}` +
    `?asset_class=${encodeURIComponent(args.asset_class)}`;
  const res = await apiFetch(path, { method: "DELETE" }, token);
  if (res.status === 204) return;
  await throwApiError("DELETE", path, res);
}

// ──── live deployment workflow ────
//
// Types + fetchers for the live-portfolio deploy/stop/kill-all/resume flow,
// plus revision-members read. Mirrors backend schemas/live.py +
// schemas/live_portfolio.py response models (drilled 2026-05-14 — see
// docs/plans/2026-05-15-live-deployment-workflow-ui-cli.md "T1" notes).
//
// Note: `getLivePositions` and `getLiveTrades` already exist above with
// stronger typing (LivePositionItem/LiveTrade) — kept intact per the
// "additive only" T1 constraint. New work in T7/T8 reuses those.

/** Response from POST /api/v1/live/start-portfolio. */
export interface PortfolioStartResponse {
  /** Deployment UUID. */
  id: string;
  deployment_slug: string;
  /**
   * One of "starting" | "building" | "ready" | "running" — any active
   * status. UI should NOT hard-reject other strings (forward compat).
   */
  status: string;
  paper_trading: boolean;
  warm_restart: boolean;
}

/**
 * Response from POST /api/v1/live/stop. Fields after `id`/`status` are
 * OPTIONAL — the idempotent already-stopped path returns only those two.
 */
export interface LiveStopResponse {
  id: string;
  status: string;
  process_status?: string;
  stop_nonce?: string;
  /** `null` when the flatness poll timed out. */
  broker_flat?: boolean | null;
  remaining_positions?: Array<Record<string, unknown>>;
}

/** Per-deployment flatness entry inside the kill-all response. */
export interface KillAllFlatnessReport {
  deployment_id: string;
  /** `null` on timeout. */
  broker_flat: boolean | null;
  remaining_positions: Array<Record<string, unknown>>;
  /** Per-deployment nonce; nested here, NOT at top level. */
  stop_nonce?: string | null;
}

/** Response from POST /api/v1/live/kill-all. */
export interface LiveKillAllResponse {
  stopped: number;
  /** Count of publish failures (NOT a kill_nonce — there's no top-level nonce). */
  failed_publish: number;
  risk_halted: boolean;
  any_non_flat: boolean;
  flatness_reports: KillAllFlatnessReport[];
}

/** Response from POST /api/v1/live/resume. */
export interface LiveResumeResponse {
  resumed: boolean;
}

/** A live portfolio (control-plane container for revisions). */
export interface LivePortfolio {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

/** An (immutable, once frozen) revision of a live portfolio. */
export interface LivePortfolioRevision {
  id: string;
  revision_number: number;
  composition_hash: string;
  is_frozen: boolean;
  created_at: string;
}

/** A frozen member row inside a portfolio revision. */
export interface LivePortfolioMemberFrozen {
  id: string;
  /** Strategy UUID. */
  strategy_id: string;
  config: Record<string, unknown>;
  instruments: string[];
  /** Decimal serialized as a string. */
  weight: string;
  order_index: number;
}

/**
 * POST /api/v1/live/start-portfolio — deploy a portfolio revision.
 *
 * `idempotencyKey` is passed via the `Idempotency-Key` header to protect
 * the deployment write from network-retry double-fires.
 */
export async function startPortfolio(
  body: {
    portfolio_revision_id: string;
    account_id: string;
    paper_trading: boolean;
    ib_login_key: string;
  },
  idempotencyKey: string,
  token?: string | null,
): Promise<PortfolioStartResponse> {
  const path = "/api/v1/live/start-portfolio";
  const res = await apiFetch(
    path,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
    },
    token,
  );
  if (!res.ok) await throwApiError("POST", path, res);
  return (await res.json()) as PortfolioStartResponse;
}

/** POST /api/v1/live/stop — stop a single deployment (idempotent). */
export async function stopDeployment(
  deploymentId: string,
  token?: string | null,
): Promise<LiveStopResponse> {
  return apiPost<LiveStopResponse>(
    "/api/v1/live/stop",
    { deployment_id: deploymentId },
    token,
  );
}

/** POST /api/v1/live/kill-all — emergency halt all deployments. */
export async function killAllLive(
  token?: string | null,
): Promise<LiveKillAllResponse> {
  return apiPost<LiveKillAllResponse>("/api/v1/live/kill-all", {}, token);
}

/** POST /api/v1/live/resume — clear the risk-halt flag. */
export async function resumeLive(
  token?: string | null,
): Promise<LiveResumeResponse> {
  return apiPost<LiveResumeResponse>("/api/v1/live/resume", {}, token);
}

/**
 * GET /api/v1/live-portfolio-revisions/{revision_id}/members — frozen
 * member rows for a portfolio revision (new endpoint from T0b).
 */
export async function getRevisionMembers(
  revisionId: string,
  token?: string | null,
): Promise<LivePortfolioMemberFrozen[]> {
  return apiGet<LivePortfolioMemberFrozen[]>(
    `/api/v1/live-portfolio-revisions/${encodeURIComponent(revisionId)}/members`,
    token,
  );
}

// =====================================================================
// T5 — UI-completeness new fetchers (Wave 2)
// Mirror of backend schemas added/extended in Wave 1 T2/T3/T4.
// =====================================================================

// ---- Alerts (GET /api/v1/alerts/) ----

export interface AlertRecord {
  type: string;
  level: string;
  title: string;
  message: string;
  /** ISO8601 UTC string from backend. */
  created_at: string;
  /**
   * iter-5 verify-e2e Issue D (P2): stable opaque id derived backend-side
   * from sha256(type|title|created_at)[:16]. Useful for client-side dedup
   * + permalink construction. UI still iterates by array index per
   * R19/R22 snapshot-into-local-state.
   */
  id: string;
}

export interface AlertListResponse {
  alerts: AlertRecord[];
  /**
   * iter-5 verify-e2e Issue D: number of records in this response.
   * Alerts don't paginate (limit-only); ``total`` == ``alerts.length``.
   */
  total: number;
}

export async function getAlerts(
  token?: string | null,
  limit: number = 50,
): Promise<AlertListResponse> {
  const q = new URLSearchParams({ limit: String(limit) });
  return apiGet<AlertListResponse>(`/api/v1/alerts/?${q.toString()}`, token);
}

// ---- Account portfolio + health (snapshot-backed) ----

export interface AccountPortfolioItem {
  // Snake-case keys MATCH the backend snapshot's ``_fetch_portfolio``
  // shape (Codex iter-1 P0 — the previous camelCase contract rendered
  // em-dashes for every position cell). Values are numeric on the wire
  // (IB returns floats); we keep the strict shape and tolerate the
  // contract evolving via optional fields.
  symbol: string;
  position: number;
  market_price?: number;
  market_value?: number;
  average_cost?: number;
  unrealized_pnl?: number;
  realized_pnl?: number;
}

export async function getAccountPortfolio(
  token?: string | null,
): Promise<AccountPortfolioItem[]> {
  return apiGet<AccountPortfolioItem[]>("/api/v1/account/portfolio", token);
}

/**
 * Closed enum per Codex iter-1 P1 — the previous ``... | string`` open
 * union was collapsed to ``string`` by TypeScript and provided zero
 * compile-time benefit. Aligned with ``SubsystemStatus.status`` values.
 */
export type AccountHealthStatus = "healthy" | "unhealthy" | "unknown";

export interface AccountHealth {
  status: AccountHealthStatus;
  gateway_connected: boolean;
  /**
   * iter-5 verify-e2e Issue G (P2): used to be a string ("1525") which
   * forced every reader to ``parseInt`` and risked truthy ``"0"`` bugs.
   * Backend `/api/v1/account/health` now returns int. Consumers can use
   * the value directly for numeric comparisons + thresholding.
   */
  consecutive_failures: number;
}

export async function getAccountHealth(
  token?: string | null,
): Promise<AccountHealth> {
  return apiGet<AccountHealth>("/api/v1/account/health", token);
}

// ---- System health aggregator (GET /api/v1/system/health) ----

export interface SubsystemStatus {
  /** "healthy" | "unhealthy" | "unknown" — backend uses string for forward-compat. */
  status: string;
  /** ISO8601 UTC timestamp of latest probe. */
  last_checked: string;
  detail: string | null;
  /** Subsystems may attach arbitrary extra fields (e.g. queue_depth, total_files). */
  [key: string]: unknown;
}

export interface SystemHealthResponse {
  subsystems: Record<string, SubsystemStatus>;
  version: string;
  /** 7-char short SHA. */
  commit_sha: string;
  uptime_seconds: number;
}

export async function getSystemHealth(
  token?: string | null,
): Promise<SystemHealthResponse> {
  return apiGet<SystemHealthResponse>("/api/v1/system/health", token);
}

// ---- Strategy CRUD extensions (PATCH / validate / DELETE) ----

export interface StrategyUpdate {
  /** PATCH body — both fields optional. Per R3, `name` is NOT editable
   *  (registry sync overwrites from disk). */
  description?: string | null;
  default_config?: Record<string, unknown> | null;
}

export async function patchStrategy(
  id: string,
  body: StrategyUpdate,
  token?: string | null,
): Promise<StrategyResponse> {
  const path = `/api/v1/strategies/${encodeURIComponent(id)}`;
  const res = await apiFetch(
    path,
    { method: "PATCH", body: JSON.stringify(body) },
    token,
  );
  if (!res.ok) await throwApiError("PATCH", path, res);
  return (await res.json()) as StrategyResponse;
}

export async function validateStrategy(
  id: string,
  token?: string | null,
): Promise<{ message: string }> {
  return apiPost<{ message: string }>(
    `/api/v1/strategies/${encodeURIComponent(id)}/validate`,
    {},
    token,
  );
}

export async function deleteStrategy(
  id: string,
  token?: string | null,
): Promise<{ message: string }> {
  const path = `/api/v1/strategies/${encodeURIComponent(id)}`;
  const res = await apiFetch(path, { method: "DELETE" }, token);
  if (!res.ok) await throwApiError("DELETE", path, res);
  return (await res.json()) as { message: string };
}

// ---- Live audits per deployment ----

/**
 * Closed enums for trader audit fields per Codex iter-1 P1 — open string
 * types let `side: "buyish"` silently typecheck on a real-money platform.
 * Backend may return additional values over time (forward-compat); we
 * accept that risk because the trader audit log surface is high-signal.
 */
export type LiveAuditSide = "BUY" | "SELL";
export type LiveAuditStatus =
  | "submitted"
  | "filled"
  | "partial"
  | "cancelled"
  | "rejected"
  | "expired";

export interface LiveAuditRow {
  id: string;
  client_order_id: string;
  instrument_id: string;
  side: LiveAuditSide;
  /** Decimal serialized as string (precision-sensitive). */
  quantity: string;
  status: LiveAuditStatus;
  strategy_code_hash: string;
  /** ISO8601 timestamp. */
  timestamp: string;
}

export interface LiveAuditsResponse {
  audits: LiveAuditRow[];
}

export async function getLiveAudits(
  deploymentId: string,
  token?: string | null,
): Promise<LiveAuditsResponse> {
  return apiGet<LiveAuditsResponse>(
    `/api/v1/live/audits/${encodeURIComponent(deploymentId)}`,
    token,
  );
}

// ---- Research job cancel ----

export async function cancelResearchJob(
  id: string,
  token?: string | null,
): Promise<ResearchJobResponse> {
  return apiPost<ResearchJobResponse>(
    `/api/v1/research/jobs/${encodeURIComponent(id)}/cancel`,
    {},
    token,
  );
}

// ---- /auth/me — user profile from backend claims (not MSAL idTokenClaims) ----

export interface UserProfile {
  id: string;
  entra_id: string;
  email: string;
  display_name: string | null;
  role: string | null;
}

export async function getUserProfile(
  token?: string | null,
): Promise<UserProfile> {
  return apiGet<UserProfile>("/api/v1/auth/me", token);
}
