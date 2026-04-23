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

/** Fetch JSON from the API, throwing ApiError on non-2xx. */
export async function apiGet<T>(
  path: string,
  token?: string | null,
): Promise<T> {
  const res = await apiFetch(path, { method: "GET" }, token);
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      // ignore
    }
    throw new ApiError(`GET ${path} failed: ${res.status}`, res.status, body);
  }
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
  if (!res.ok) {
    let errBody: unknown = null;
    try {
      errBody = await res.json();
    } catch {
      // ignore
    }
    throw new ApiError(
      `POST ${path} failed: ${res.status}`,
      res.status,
      errBody,
    );
  }
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
