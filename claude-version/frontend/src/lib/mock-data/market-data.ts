/**
 * Mock OHLCV data for the Market Data page.
 */

export interface OHLCVBar {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export const symbols = [
  { value: "AAPL", label: "AAPL - Apple Inc." },
  { value: "MSFT", label: "MSFT - Microsoft Corp." },
  { value: "SPY", label: "SPY - S&P 500 ETF" },
  { value: "ES", label: "ES - E-mini S&P 500" },
  { value: "BTC", label: "BTC - Bitcoin" },
] as const;

export function generateOHLCV(symbol: string, days: number): OHLCVBar[] {
  const bars: OHLCVBar[] = [];

  const basePrices: Record<string, number> = {
    AAPL: 185,
    MSFT: 410,
    SPY: 500,
    ES: 5040,
    BTC: 62000,
  };

  let price = basePrices[symbol] ?? 100;
  const volatility = symbol === "BTC" ? 0.03 : 0.015;
  const now = new Date();

  for (let i = days - 1; i >= 0; i--) {
    const date = new Date(now);
    date.setDate(date.getDate() - i);

    // Skip weekends for non-crypto
    if (symbol !== "BTC" && (date.getDay() === 0 || date.getDay() === 6)) {
      continue;
    }

    const change = (Math.random() - 0.48) * volatility * price;
    const open = price;
    const close = price + change;
    const high =
      Math.max(open, close) + Math.random() * volatility * price * 0.5;
    const low =
      Math.min(open, close) - Math.random() * volatility * price * 0.5;
    const volume = Math.round(1_000_000 + Math.random() * 5_000_000);

    bars.push({
      time: date.toISOString().split("T")[0],
      open: Math.round(open * 100) / 100,
      high: Math.round(high * 100) / 100,
      low: Math.round(low * 100) / 100,
      close: Math.round(close * 100) / 100,
      volume,
    });

    price = close;
  }

  return bars;
}
