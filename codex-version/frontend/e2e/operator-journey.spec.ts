import { expect, test, type Page } from "@playwright/test";

type GraduationCandidate = {
  id: string;
  promotion_id: string;
  report_id: string;
  created_at: string;
  updated_at: string;
  stage: string;
  notes?: string | null;
  strategy_id: string;
  strategy_name: string;
  strategy_path: string;
  instruments: string[];
  config: Record<string, unknown>;
  selection: {
    kind?: string;
    result_index?: number | null;
    window_index?: number | null;
    metrics?: Record<string, number>;
  };
  paper_trading: boolean;
  live_url: string;
  portfolio_url: string;
};

async function installOperatorJourneyRoutes(page: Page) {
  let backtestHistory = [
    {
      id: "bt-warmup-1",
      status: "completed",
      created_at: "2026-04-08T14:25:00Z",
    },
  ];

  const backtestResults = {
    status: { id: "bt-journey-1", status: "completed", progress: 100 },
    results: {
      id: "bt-journey-1",
      status: "completed",
      metrics: {
        sharpe: 1.64,
        sortino: 2.28,
        total_return: 0.18,
        max_drawdown: 0.06,
      },
      trades: [
        {
          executed_at: "2026-03-31T14:30:00Z",
          instrument: "SPY.EQUS",
          side: "BUY",
          quantity: 100,
          price: 512.2,
          pnl: 0,
        },
        {
          executed_at: "2026-04-02T14:30:00Z",
          instrument: "SPY.EQUS",
          side: "SELL",
          quantity: 100,
          price: 518.4,
          pnl: 620,
        },
      ],
    },
    analytics: {
      id: "bt-journey-1",
      metrics: {
        sharpe: 1.64,
        sortino: 2.28,
        total_return: 0.18,
        max_drawdown: 0.06,
      },
      series: [
        { timestamp: "2026-03-31T14:30:00Z", returns: 0, equity: 1_000_000, drawdown: 0 },
        { timestamp: "2026-04-01T14:30:00Z", returns: 0.008, equity: 1_008_000, drawdown: -0.005 },
        { timestamp: "2026-04-02T14:30:00Z", returns: 0.011, equity: 1_019_000, drawdown: -0.003 },
      ],
      report_url: "/api/v1/backtests/bt-journey-1/report",
    },
  };

  const researchJobs = [
    {
      id: "wf-job-1",
      job_type: "walk_forward",
      status: "completed",
      progress: 100,
      created_at: "2026-04-08T14:31:00Z",
      strategy_id: "strategy-slope",
      strategy_name: "user.slope_ma_breakout",
      strategy_path: "/repo/strategies/user/slope_ma_breakout.py",
      instruments: ["NQ.v.0"],
      objective: "sharpe",
      report_id: "wf-report-1",
    },
  ];

  const researchReportSummary = {
    id: "wf-report-1",
    mode: "walk_forward",
    generated_at: "2026-04-08T14:35:00Z",
    strategy_name: "user.slope_ma_breakout",
    instruments: ["NQ.v.0"],
    objective: "sharpe",
    summary: {
      window_count: 3,
      successful_test_windows: 3,
    },
    best_config: {
      moving_average_period: 30,
      slope_lookback: 5,
      breakout_buffer_atr: 0.1,
    },
    best_metrics: {
      sharpe: 1.42,
      total_return: 0.21,
      max_drawdown: 0.07,
    },
    candidate_count: 3,
  };

  const researchReportDetail = {
    summary: researchReportSummary,
    report: {
      mode: "walk_forward",
      generated_at: "2026-04-08T14:35:00Z",
      objective: "sharpe",
      instruments: ["NQ.v.0"],
      windows: [
        {
          train_start: "2025-01-01",
          train_end: "2025-06-30",
          test_start: "2025-07-01",
          test_end: "2025-09-30",
          best_train_result: {
            config: {
              moving_average_period: 30,
              slope_lookback: 5,
              breakout_buffer_atr: 0.1,
            },
            metrics: { sharpe: 1.71, total_return: 0.16 },
            error: null,
          },
          test_result: {
            metrics: { sharpe: 1.42, total_return: 0.11 },
            error: null,
          },
        },
      ],
    },
  };

  let candidate: GraduationCandidate = {
    id: "candidate-journey-1",
    promotion_id: "promotion-journey-1",
    report_id: "wf-report-1",
    created_at: "2026-04-08T14:36:00Z",
    updated_at: "2026-04-08T14:36:00Z",
    stage: "paper_candidate",
    notes: "Promoted from walk-forward window 1",
    strategy_id: "strategy-slope",
    strategy_name: "user.slope_ma_breakout",
    strategy_path: "/repo/strategies/user/slope_ma_breakout.py",
    instruments: ["NQ.v.0"],
    config: {
      moving_average_period: 30,
      slope_lookback: 5,
      breakout_buffer_atr: 0.1,
    },
    selection: {
      kind: "walk_forward",
      window_index: 0,
      metrics: { sharpe: 1.42, sortino: 2.01, total_return: 0.11, win_rate: 0.58 },
    },
    paper_trading: true,
    live_url: "/live?candidate_id=candidate-journey-1",
    portfolio_url: "/portfolio?candidate_id=candidate-journey-1",
  };

  let portfolios = [] as Array<{
    id: string;
    name: string;
    description?: string | null;
    objective: string;
    base_capital: number;
    requested_leverage: number;
    downside_target?: number | null;
    benchmark_symbol?: string | null;
    allocations: Array<{
      candidate_id: string;
      strategy_name: string;
      instruments: string[];
      weight: number;
    }>;
  }>;

  let portfolioRuns = [] as Array<{
    id: string;
    portfolio_id: string;
    portfolio_name: string;
    status: string;
    start_date: string;
    end_date: string;
    max_parallelism: number;
    metrics: Record<string, number>;
    series: Array<{ timestamp: string; equity: number; drawdown: number; returns: number }>;
    allocations: Array<{
      candidate_id: string;
      strategy_name: string;
      instruments: string[];
      weight: number;
      metrics: Record<string, number>;
    }>;
    report_path: string;
  }>;

  let deployments = [] as Array<{
    id: string;
    strategy: string;
    status: string;
    started_at?: string;
    daily_pnl?: number;
    open_positions?: number;
    open_orders?: number;
    updated_at?: string;
  }>;
  let livePositions = [] as Array<{
    deployment_id?: string;
    instrument: string;
    quantity: number;
    avg_price: number;
    current_price?: number;
    unrealized_pnl: number;
    market_value: number;
  }>;
  let liveOrders = [] as Array<{
    deployment_id?: string;
    instrument?: string;
    side?: string;
    quantity?: number;
    status?: string;
    order_type?: string;
    price?: number;
    ts_last?: string;
  }>;
  let liveTrades = [] as Array<{
    id?: string;
    deployment_id?: string;
    instrument: string;
    side?: string;
    quantity?: number;
    price?: number;
    pnl?: number;
    executed_at?: string;
  }>;
  let riskSnapshot = {
    halted: false,
    current_pnl: 0,
    notional_exposure: 0,
    portfolio_value: 1_000_000,
    margin_used: 0,
    position_count: 0,
    updated_at: "2026-04-08T14:38:00Z",
  };
  let accountSummary = {
    net_liquidation: 1_000_000,
    equity_with_loan_value: 995_000,
    buying_power: 2_000_000,
    margin_used: 0,
    initial_margin_requirement: 0,
    maintenance_margin_requirement: 0,
    available_funds: 995_000,
    excess_liquidity: 995_000,
    sma: 50_000,
    gross_position_value: 0,
    cushion: 0.995,
    unrealized_pnl: 0,
  };
  let brokerSnapshot = {
    connected: true,
    mock_mode: false,
    generated_at: "2026-04-08T14:38:00Z",
    positions: [] as Array<Record<string, unknown>>,
    open_orders: [] as Array<Record<string, unknown>>,
  };

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    expect(request.headers()["x-api-key"]).toBe("msai-dev-key");

    if (method === "GET" && path === "/api/v1/strategies/") {
      await route.fulfill({
        json: [
          { id: "strategy-slope", name: "user.slope_ma_breakout" },
          { id: "strategy-breakout", name: "example.donchian_breakout" },
        ],
      });
      return;
    }

    if (method === "GET" && path === "/api/v1/strategies/strategy-slope") {
      await route.fulfill({
        json: {
          id: "strategy-slope",
          name: "user.slope_ma_breakout",
          default_config: {
            moving_average_period: 30,
            slope_lookback: 5,
            breakout_buffer_atr: 0.1,
          },
        },
      });
      return;
    }

    if (method === "GET" && path === "/api/v1/backtests/history") {
      await route.fulfill({ json: backtestHistory });
      return;
    }

    if (method === "POST" && path === "/api/v1/backtests/run") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      expect(payload.strategy_id).toBe("strategy-slope");
      backtestHistory = [
        {
          id: "bt-journey-1",
          status: "completed",
          created_at: "2026-04-08T14:30:00Z",
        },
        ...backtestHistory,
      ];
      await route.fulfill({ json: { job_id: "bt-journey-1" } });
      return;
    }

    if (method === "GET" && path === "/api/v1/backtests/bt-journey-1/status") {
      await route.fulfill({ json: backtestResults.status });
      return;
    }

    if (method === "GET" && path === "/api/v1/backtests/bt-journey-1/results") {
      await route.fulfill({ json: backtestResults.results });
      return;
    }

    if (method === "GET" && path === "/api/v1/backtests/bt-journey-1/analytics") {
      await route.fulfill({ json: backtestResults.analytics });
      return;
    }

    if (method === "GET" && path === "/api/v1/research/reports") {
      await route.fulfill({ json: [researchReportSummary] });
      return;
    }

    if (method === "GET" && path === "/api/v1/research/jobs") {
      await route.fulfill({ json: researchJobs });
      return;
    }

    if (method === "POST" && path === "/api/v1/research/walk-forward") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      expect(payload.strategy_id).toBe("strategy-slope");
      expect(payload.mode).toBe("rolling");
      await route.fulfill({ json: { job_id: "wf-job-1", status: "pending" } });
      return;
    }

    if (method === "GET" && path === "/api/v1/research/reports/wf-report-1") {
      await route.fulfill({ json: researchReportDetail });
      return;
    }

    if (method === "POST" && path === "/api/v1/research/promotions") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      expect(payload.report_id).toBe("wf-report-1");
      expect(payload.window_index).toBe(0);
      await route.fulfill({
        json: {
          id: "promotion-journey-1",
          report_id: "wf-report-1",
          created_at: "2026-04-08T14:36:00Z",
          created_by: "user-1",
          paper_trading: true,
          strategy_id: "strategy-slope",
          strategy_name: "user.slope_ma_breakout",
          instruments: ["NQ.v.0"],
          config: candidate.config,
          selection: {
            kind: "walk_forward",
            result_index: null,
            window_index: 0,
          },
          live_url: "/live?promotion_id=promotion-journey-1",
        },
      });
      return;
    }

    if (method === "POST" && path === "/api/v1/graduation/candidates") {
      await route.fulfill({ json: candidate });
      return;
    }

    if (method === "GET" && path === "/api/v1/graduation/candidates") {
      await route.fulfill({ json: [candidate] });
      return;
    }

    if (method === "GET" && path === "/api/v1/graduation/candidates/candidate-journey-1") {
      await route.fulfill({ json: candidate });
      return;
    }

    if (method === "POST" && path === "/api/v1/graduation/candidates/candidate-journey-1/stage") {
      const payload = request.postDataJSON() as { stage: string; notes?: string | null };
      candidate = {
        ...candidate,
        stage: payload.stage,
        notes: payload.notes ?? candidate.notes,
        updated_at: "2026-04-08T14:39:00Z",
      };
      await route.fulfill({ json: candidate });
      return;
    }

    if (method === "GET" && path === "/api/v1/portfolios") {
      await route.fulfill({ json: portfolios });
      return;
    }

    if (method === "POST" && path === "/api/v1/portfolios") {
      portfolios = [
        {
          id: "portfolio-journey-1",
          name: "Core Trend Portfolio",
          description: "Graduated sleeves ready for paper capital",
          objective: "maximize_sharpe",
          base_capital: 1_000_000,
          requested_leverage: 1,
          downside_target: null,
          benchmark_symbol: "SPY.EQUS",
          allocations: [
            {
              candidate_id: candidate.id,
              strategy_name: candidate.strategy_name,
              instruments: candidate.instruments,
              weight: 1,
            },
          ],
        },
      ];
      await route.fulfill({ json: portfolios[0] });
      return;
    }

    if (method === "GET" && path === "/api/v1/portfolios/runs") {
      await route.fulfill({ json: portfolioRuns });
      return;
    }

    if (method === "POST" && path === "/api/v1/portfolios/portfolio-journey-1/runs") {
      portfolioRuns = [
        {
          id: "portfolio-run-1",
          portfolio_id: "portfolio-journey-1",
          portfolio_name: "Core Trend Portfolio",
          status: "completed",
          start_date: "2026-03-31",
          end_date: "2026-04-03",
          max_parallelism: 2,
          metrics: {
            sharpe: 1.73,
            sortino: 2.41,
            alpha: 0.12,
            beta: 0.81,
            total_return: 0.15,
            max_drawdown: 0.05,
            effective_leverage: 1.0,
          },
          series: [
            { timestamp: "2026-03-31T14:30:00Z", equity: 1_000_000, drawdown: 0, returns: 0 },
            { timestamp: "2026-04-01T14:30:00Z", equity: 1_011_000, drawdown: -0.004, returns: 0.011 },
            { timestamp: "2026-04-02T14:30:00Z", equity: 1_025_000, drawdown: -0.003, returns: 0.014 },
          ],
          allocations: [
            {
              candidate_id: candidate.id,
              strategy_name: candidate.strategy_name,
              instruments: candidate.instruments,
              weight: 1,
              metrics: { sharpe: 1.42, total_return: 0.11 },
            },
          ],
          report_path: "/tmp/portfolio-report.html",
        },
      ];
      await route.fulfill({ json: portfolioRuns[0] });
      return;
    }

    if (method === "GET" && path === "/api/v1/live/status") {
      await route.fulfill({ json: deployments });
      return;
    }

    if (method === "GET" && path === "/api/v1/live/positions") {
      await route.fulfill({ json: livePositions });
      return;
    }

    if (method === "GET" && path === "/api/v1/live/orders") {
      await route.fulfill({ json: liveOrders });
      return;
    }

    if (method === "GET" && path === "/api/v1/live/trades") {
      await route.fulfill({ json: liveTrades });
      return;
    }

    if (method === "GET" && path === "/api/v1/live/risk-status") {
      await route.fulfill({ json: riskSnapshot });
      return;
    }

    if (method === "GET" && path === "/api/v1/account/summary") {
      await route.fulfill({ json: accountSummary });
      return;
    }

    if (method === "GET" && path === "/api/v1/account/snapshot") {
      await route.fulfill({ json: brokerSnapshot });
      return;
    }

    if (method === "POST" && path === "/api/v1/live/start") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      expect(payload.strategy_id).toBe("strategy-slope");
      deployments = [
        {
          id: "dep-live-1",
          strategy: "user.slope_ma_breakout",
          status: "running",
          started_at: "2026-04-08T14:41:00Z",
          daily_pnl: 2350,
          open_positions: 1,
          open_orders: 1,
          updated_at: "2026-04-08T14:41:05Z",
        },
      ];
      livePositions = [
        {
          deployment_id: "dep-live-1",
          instrument: "NQ.v.0",
          quantity: 1,
          avg_price: 18250,
          current_price: 18278,
          unrealized_pnl: 560,
          market_value: 36556,
        },
      ];
      liveOrders = [
        {
          deployment_id: "dep-live-1",
          instrument: "NQ.v.0",
          side: "BUY",
          quantity: 1,
          status: "Submitted",
          order_type: "MARKET",
        },
      ];
      liveTrades = [
        {
          id: "trade-live-1",
          deployment_id: "dep-live-1",
          instrument: "NQ.v.0",
          side: "BUY",
          quantity: 1,
          price: 18250,
          pnl: 0,
          executed_at: "2026-04-08T14:41:03Z",
        },
      ];
      riskSnapshot = {
        halted: false,
        current_pnl: 2350,
        notional_exposure: 36556,
        portfolio_value: 1_002_350,
        margin_used: 18500,
        position_count: 1,
        updated_at: "2026-04-08T14:41:05Z",
      };
      accountSummary = {
        ...accountSummary,
        margin_used: 18_500,
        initial_margin_requirement: 18_500,
        maintenance_margin_requirement: 17_500,
        available_funds: 976_500,
        excess_liquidity: 957_500,
        gross_position_value: 36_556,
        cushion: 0.955,
        unrealized_pnl: 2_350,
      };
      brokerSnapshot = {
        connected: true,
        mock_mode: false,
        generated_at: "2026-04-08T14:41:05Z",
        positions: [
          {
            account_id: "DUP733211",
            instrument: "NQ.v.0",
            quantity: 1,
            market_value: 36556,
            unrealized_pnl: 560,
          },
        ],
        open_orders: [
          {
            account_id: "DUP733211",
            instrument: "NQ.v.0",
            status: "Submitted",
            side: "BUY",
            quantity: 1,
            remaining: 0,
          },
        ],
      };
      await route.fulfill({ json: { deployment_id: "dep-live-1" } });
      return;
    }

    if (method === "POST" && path === "/api/v1/live/stop") {
      deployments = [];
      livePositions = [];
      liveOrders = [];
      liveTrades = [];
      riskSnapshot = {
        ...riskSnapshot,
        current_pnl: 0,
        notional_exposure: 0,
        position_count: 0,
        margin_used: 0,
      };
      brokerSnapshot = {
        ...brokerSnapshot,
        open_orders: [],
        positions: [],
      };
      await route.fulfill({ json: { status: "stopped" } });
      return;
    }

    await route.fulfill({ status: 404, json: { detail: `Unhandled route: ${path}` } });
  });
}

test.describe("operator browser journey", () => {
  test("covers login, backtest, cross-validation, graduation, portfolio, and live launch", async ({ page }) => {
    await installOperatorJourneyRoutes(page);

    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "API Key Test Mode" })).toBeVisible();

    await page.goto("/backtests");
    await expect(page.getByRole("heading", { name: "Backtest Runner" })).toBeVisible();
    await page.getByRole("button", { name: "Run Backtest" }).click();
    await expect(page).toHaveURL(/\/backtests\/bt-journey-1$/);
    await expect(page.getByText("Job bt-journey-1")).toBeVisible();
    await expect(page.getByRole("link", { name: "Open HTML Report" })).toBeVisible();
    await expect(page.getByRole("cell", { name: "SPY.EQUS" }).first()).toBeVisible();

    await page.goto("/research");
    await expect(page.getByRole("heading", { name: "Research Console" })).toBeVisible();
    await page.getByLabel("Mode").selectOption("walk_forward");
    await page.getByRole("button", { name: "Queue Research Job" }).click();
    await expect(page.getByText("Queued walk forward job wf-job-1")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Walk-Forward Windows" })).toBeVisible();
    await page.getByRole("button", { name: "Graduate" }).first().click();
    await expect(page.getByText("Graduation candidate created")).toBeVisible();
    await page.getByRole("link", { name: "Open in Graduation" }).click();

    await expect(page).toHaveURL(/\/graduation\?candidate_id=candidate-journey-1/);
    await expect(page.getByRole("heading", { name: /Turn research winners into paper and live sleeves/i })).toBeVisible();
    await page.getByLabel("Stage").selectOption("paper_running");
    await page.getByRole("button", { name: "Apply Stage Update" }).click();
    await expect(page.getByText("Candidate moved to Paper Running.")).toBeVisible();

    await page.getByRole("link", { name: "Open Portfolio Lab" }).click();
    await expect(page).toHaveURL(/\/portfolio\?candidate_id=candidate-journey-1/);
    await expect(page.getByRole("heading", { name: /Model portfolio allocation/i })).toBeVisible();
    await page.getByRole("button", { name: "Create Portfolio" }).click();
    await expect(page.getByText("Portfolio Core Trend Portfolio created.")).toBeVisible();
    await page.getByRole("button", { name: "Run Portfolio" }).click();
    await expect(page.getByText("Portfolio run portfolio-run-1 queued.")).toBeVisible();
    await expect(page.getByText("Selected Run Detail")).toBeVisible();

    await page.goBack();
    await expect(page).toHaveURL(/\/graduation\?candidate_id=candidate-journey-1/);
    await page.getByLabel("Stage").selectOption("live_candidate");
    await page.getByRole("button", { name: "Apply Stage Update" }).click();
    await expect(page.getByText("Candidate moved to Live Candidate.")).toBeVisible();
    await page.getByRole("link", { name: "Open Live Desk" }).click();

    await expect(page).toHaveURL(/\/live\?candidate_id=candidate-journey-1/);
    await expect(page.getByText("Graduation candidate loaded")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Interactive Brokers status" })).toBeVisible();
    await page.getByLabel("Deployment Mode").selectOption("live");
    await page.getByRole("button", { name: "Start Live Deployment" }).click();

    await expect(page.getByRole("cell", { name: "dep-live-1" }).first()).toBeVisible();
    await expect(page.getByRole("cell", { name: "NQ.v.0" }).first()).toBeVisible();
    await expect(page.getByText("Connected").first()).toBeVisible();
    await expect(page.getByText("Broker Truth")).toBeVisible();
  });

  test("keeps the live desk usable on a narrow viewport", async ({ page }) => {
    await installOperatorJourneyRoutes(page);
    await page.setViewportSize({ width: 430, height: 932 });

    await page.goto("/live?candidate_id=candidate-journey-1");
    await expect(page.getByRole("heading", { name: "Interactive Brokers status" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Deploy Strategy" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "IB positions and working orders" })).toBeVisible();
  });
});
