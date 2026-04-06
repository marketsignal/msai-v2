/**
 * Mock data for the Data Management page.
 */

export interface StorageCategory {
  name: string;
  sizeBytes: number;
  label: string;
}

export interface IngestionStatus {
  lastRun: string;
  nextScheduled: string;
  status: "success" | "failed" | "running";
  duration: string;
  recordsProcessed: number;
}

export interface DataSymbol {
  symbol: string;
  assetClass: string;
  lastUpdated: string;
  rowCount: number;
  sizeBytes: number;
}

export const storageCategories: StorageCategory[] = [
  { name: "Stocks", sizeBytes: 500_000_000, label: "500 MB" },
  { name: "Indexes", sizeBytes: 500_000_000, label: "500 MB" },
  { name: "Futures", sizeBytes: 1_700_000_000, label: "1.7 GB" },
  { name: "Options", sizeBytes: 15_000_000_000, label: "15 GB" },
  { name: "Crypto", sizeBytes: 50_000_000, label: "50 MB" },
];

export const ingestionStatus: IngestionStatus = {
  lastRun: "2026-02-25T06:00:00Z",
  nextScheduled: "2026-02-26T06:00:00Z",
  status: "success",
  duration: "12m 34s",
  recordsProcessed: 2_456_789,
};

export const dataSymbols: DataSymbol[] = [
  {
    symbol: "AAPL",
    assetClass: "Stock",
    lastUpdated: "2026-02-25T16:00:00Z",
    rowCount: 2_520_000,
    sizeBytes: 45_000_000,
  },
  {
    symbol: "MSFT",
    assetClass: "Stock",
    lastUpdated: "2026-02-25T16:00:00Z",
    rowCount: 2_520_000,
    sizeBytes: 44_500_000,
  },
  {
    symbol: "SPY",
    assetClass: "ETF",
    lastUpdated: "2026-02-25T16:00:00Z",
    rowCount: 3_150_000,
    sizeBytes: 58_000_000,
  },
  {
    symbol: "ES",
    assetClass: "Futures",
    lastUpdated: "2026-02-25T17:00:00Z",
    rowCount: 8_400_000,
    sizeBytes: 350_000_000,
  },
  {
    symbol: "NQ",
    assetClass: "Futures",
    lastUpdated: "2026-02-25T17:00:00Z",
    rowCount: 7_800_000,
    sizeBytes: 320_000_000,
  },
  {
    symbol: "BTC",
    assetClass: "Crypto",
    lastUpdated: "2026-02-25T23:59:00Z",
    rowCount: 1_200_000,
    sizeBytes: 25_000_000,
  },
  {
    symbol: "ETH",
    assetClass: "Crypto",
    lastUpdated: "2026-02-25T23:59:00Z",
    rowCount: 980_000,
    sizeBytes: 20_000_000,
  },
  {
    symbol: "TSLA",
    assetClass: "Stock",
    lastUpdated: "2026-02-25T16:00:00Z",
    rowCount: 1_890_000,
    sizeBytes: 35_000_000,
  },
  {
    symbol: "AMZN",
    assetClass: "Stock",
    lastUpdated: "2026-02-25T16:00:00Z",
    rowCount: 2_100_000,
    sizeBytes: 40_000_000,
  },
  {
    symbol: "GOOGL",
    assetClass: "Stock",
    lastUpdated: "2026-02-25T16:00:00Z",
    rowCount: 2_050_000,
    sizeBytes: 39_000_000,
  },
];
