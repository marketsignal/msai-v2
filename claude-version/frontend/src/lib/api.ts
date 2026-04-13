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

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
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

export interface StrategyResponse {
  id: string;
  name: string;
  description: string;
  strategy_class: string;
  code_hash: string;
  file_path: string;
  config_schema: Record<string, unknown> | null;
  default_config: Record<string, unknown> | null;
  created_at: string;
}

export interface StrategyListResponse {
  items: StrategyResponse[];
  total: number;
}

export interface BacktestHistoryItem {
  id: string;
  strategy_id: string;
  status: "pending" | "running" | "completed" | "failed";
  start_date: string;
  end_date: string;
  created_at: string;
}

export interface BacktestHistoryResponse {
  items: BacktestHistoryItem[];
  total: number;
}

export interface BacktestStatusResponse {
  id: string;
  status: "pending" | "running" | "completed" | "failed";
  progress: number;
  started_at: string | null;
  completed_at: string | null;
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

export interface BacktestResultsResponse {
  id: string;
  metrics: BacktestMetrics | null;
  trade_count: number;
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
