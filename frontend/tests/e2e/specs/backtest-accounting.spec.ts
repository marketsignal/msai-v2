/** Focused transformation checks, not browser/E2E acceptance. */
import { expect, test } from "@playwright/test";
import {
  cumulativeReturnData,
  formatBacktestPercent,
} from "../../../src/components/backtests/results-charts";

test("first-session loss and gain use opening capital, not closing equity", () => {
  for (const [equity, expected] of [[90, -10], [110, 10], [100, 0]]) {
    const result = cumulativeReturnData(
      [{ date: "2025-01-02", equity, drawdown: 0, daily_return: 0 }],
      100,
    );
    expect(result[0].cum_return_pct).toBeCloseTo(expected, 10);
  }
});

test("capital baseline persists across days, including a total first-day loss", () => {
  const result = cumulativeReturnData(
    [90, 99].map((equity, index) => ({
      date: `2025-01-0${index + 2}`, equity, drawdown: 0, daily_return: 0,
    })),
    100,
  );
  expect(result.map((point) => point.cum_return_pct)).toEqual([
    expect.closeTo(-10, 10), expect.closeTo(-1, 10),
  ]);
  expect(cumulativeReturnData(
    [{ date: "2025-01-02", equity: 0, drawdown: -1, daily_return: -1 }], 100,
  )[0].cum_return_pct).toBe(-100);
});

test("legacy series retains its relative curve without inventing opening capital", () => {
  const result = cumulativeReturnData(
    [90, 99].map((equity, index) => ({
      date: `2025-01-0${index + 2}`, equity, drawdown: 0, daily_return: 0,
    })),
    null,
  );
  expect(result.map((point) => point.cum_return_pct)).toEqual([
    0, expect.closeTo(10, 10),
  ]);
  expect(cumulativeReturnData([], null)).toEqual([]);
});

test("small nonzero gains and losses stay distinct from zero", () => {
  expect(formatBacktestPercent(-0.000053)).toBe("-0.0000530%");
  expect(formatBacktestPercent(0.000038)).toBe("+0.0000380%");
  expect(formatBacktestPercent(0)).toBe("0.00%");
  expect(formatBacktestPercent(12.345)).toBe("+12.35%");
  expect(formatBacktestPercent(-2.5)).toBe("-2.50%");
  expect(formatBacktestPercent(-0.000000001)).toBe("-1.00e-9%");
});
