# Research Brief — Capitulation-Reversion Strategy (SPY/ES)

**Date:** 2026-06-02
**Author:** Claude (Opus 4.8) + Codex (gpt-5.5, xhigh) dual-engine research
**Status:** Pre-implementation research. Feeds `/new-feature capitulation-reversion`.
**Origin:** Operator (Pablo) research thread on (a) whether Databento Plus is needed for alpha [verdict: no — see `memory/reference_databento_plan_standard_sufficient.md`], (b) ES→SPY lead-lag, (c) the actual target idea: statistically detect the bottom of a multi-day/multi-week drawdown and trade the mean-reversion bounce, using standard-deviation statistics + futures volume.

---

## 1. Idea evolution (what we rejected and why)

### Rejected: overnight ES → SPY opening-gap

The first candidate was "overnight ES return predicts the SPY opening gap." **Rejected by the operator's own (correct) objection:** the overnight move has _already happened_ by the open, so buying the open captures nothing. The only residual edge is betting on post-open behavior (gap fade/fill vs continuation) — a thin, crowded micro-reversion bet. Demoted to a possible _feature_, not a strategy.

### Rejected as a core edge: ES order-flow / book lead-lag (HFT territory)

- ES dominates S&P price discovery historically — Budish-Cramton-Shim: ES initiated **~88.6%** of ES-SPY arb opportunities (2005-11). Modern evidence is more mixed (SPY's share rose post-2007; ES still dominates in high-vol regimes).
- BUT the exploitable lead-lag window **collapsed from ~97ms (2005) to ~7ms (2011)**, today single-digit-to-low-tens of ms. Return correlation ~0.10 @ 10ms, ~0.008 @ 1ms.
- **~100ms IB-Gateway routing is far too slow** — the price-level lead is pure HFT. Cross-asset OFI predicts only at ~1-min horizons, decaying within minutes.
- **Conclusion:** not capturable on a non-colocated retail/small-fund stack.

---

## 2. The chosen idea: capitulation-reversion (the operator's intuition, validated)

> After a multi-day/multi-week index drawdown, detect statistical selling-exhaustion (std-dev stretch + capitulation volume + fear extreme) and trade the mean-reversion bounce.

### Why it makes money — the economic mechanism (this is the real edge)

**Liquidity provision during forced selling.** When panic/forced sellers dump, liquidity providers only absorb the flow at a discount; the subsequent bounce is the compensation for providing that liquidity.

- **Lehmann (1990):** weekly index losers reverse the following week — liquidity pressure, not fundamentals.
- **Campbell-Grossman-Wang (1993):** **high-volume declines are more likely to reverse**, because the selling is liquidity-driven (temporary) rather than information-driven (permanent). → _This is why futures VOLUME belongs in the signal — the operator's instinct was correct._

### Why index, not single stocks

Broad indices revert after panic because idiosyncratic (permanent) news diversifies away. Single names _drift_ — Collin-Dufresne-Daniel estimate **~90% of large single-stock shocks are permanent**, only ~10% temporary. **Trade SPY / ES / MES, not individual names, for this edge.**

### Confirmation layers

- **Fear:** VIX spike >30 historically reverts <20 within 1-3 months; **inverted VIX term structure (VIX/VIX3M > 1, or VX1 > VX2) after a multi-week selloff** is a bottom signal — _in a bull context, not a structural bear_.
- **Breadth:** McClellan Oscillator < -150/-200; % new-lows spike then contract.
- **Capitulation fingerprint:** volume **2-5x the 20-day average** + long lower wick + close off the lows + RSI extreme; confirmed when the _next_ bar holds a higher low.

---

## 3. Honest caveats (must be internalized before building)

1. **Small, negatively-skewed, decaying edge.** Published recipes (RSI(2)<10, Bollinger bands) are partly arbitraged — McLean-Pontiff: anomalies decline **~58% post-publication**. The _durable_ part is the liquidity-provision mechanism; the _fragile_ part is any specific fitted threshold.
2. **Fat tails are the killer.** You _will_ buy dips that keep falling (2008, 2022). Dip-buyers die from rare trend-crashes, not low hit-rate. **Risk management IS the strategy.** A 200-day/10-month regime filter is primarily _risk reduction_, not return enhancement (Faber).
3. **Realistic expectation:** disciplined, this can beat buy-and-hold on **drawdown and Sharpe**; it likely does **not** beat it on **raw CAGR** without leverage or a complementary strategy. It is a risk-adjusted-return play.

---

## 4. Data decision (closes the Databento-tier thread)

The signal lives in **daily bars + ES volume + VIX — all free L0 data on Databento Standard.**

- Order-flow (L1/L2) helps **execution and absorption confirmation only**, NOT the core edge at a multi-day horizon. Andersen-Bondarenko: order-flow toxicity (VPIN) adds no incremental predictive power after controls.
- **Verdict: Standard tier is sufficient. No Plus, no Unlimited.** (See `memory/reference_databento_plan_standard_sufficient.md`.)

| Series                       | Schema                       | Tier         | Notes                                                                             |
| ---------------------------- | ---------------------------- | ------------ | --------------------------------------------------------------------------------- |
| SPY daily (+ 1-min for exec) | `ohlcv-1d` / `ohlcv-1m` (L0) | free         | equities history floor: EQUS.MINI starts 2023-03-28; older via historical dataset |
| ES front-month daily + 1-min | `ohlcv-1d` / `ohlcv-1m` (L0) | free, 15 yr  | continuous-contract roll via `instrument_aliases` effective-date windowing        |
| VIX / VIX3M                  | index level                  | free/derived | term-structure ratio as a state variable                                          |

---

## 5. Buildable signal spec (convergent design — both engines)

Trade **SPY / MES / ES**. Daily bars for the signal; 1-min ES for confirmation/execution.

**Setup score — enter long only if score >= 4 of 6:**

1. **Regime:** `Close > SMA200` and SMA200 slope > 0. _(If false → max 25% size + stronger confirmation required. This is the 2008/2022 survival filter.)_
2. **Stretch (std-dev):** `z20 = (Close - SMA20) / stdev(Close,20) <= -2.0`, or close below lower 20,2 Bollinger band.
3. **Washout:** `RSI(2) <= 5-10`, or 3 down-closes in 4 sessions.
4. **ES capitulation volume:** ES RTH volume z-score >= +1.5 vs 20-day, AND daily range >= 1.5 x ATR14.
5. **Stabilization:** close in upper half of daily range, OR next session reclaims prior VWAP / prior RTH high. _(The "higher low holds" — stops knife-catching.)_
6. **Fear/breadth:** VIX z20 >= +1.5, OR VIX/VIX3M > 1 (term inversion); plus McClellan < -150/-200 or new-lows spike-then-contract.

**Entry:** 1/3 size after setup close (only if close is off the lows); else next RTH after ES holds above setup low for 30-60 min and reclaims VWAP.
**Scale:** add 1/3 only on a lower low with confirmation intact. Max 3 entries. **No martingale.**
**Exit:** partial at SMA5 / `z20 >= -0.5`; full exit at `RSI(2)>70`, `Close > SMA10`, `z20 >= 0`, or after 8 trading days. **Hard stop:** close below setup-low - 0.5 x ATR14.
**Risk:** 25-50 bps equity per idea; vol-target notional down as ATR/VIX rises; disable full-size longs below a falling SMA200; **use MES before ES** until account size makes ES risk trivial.

---

## 6. Implementation plan — measurement gate FIRST

### Phase 1 — falsification gate (cheap, ~free, do before any strategy code)

Over all available SPY daily + ES volume history, compute the 6-factor score and measure **forward 1/3/5/10-day SPY returns conditional on score >= 4, vs unconditional baseline.** Report hit-rate, mean/median forward return, and conditional Sharpe by score bucket.
**Kill criterion:** if conditional forward returns and hit-rate are not clearly better than unconditional → the edge isn't there; do not build the strategy. One day of analysis answers it.

### Phase 2 — NautilusTrader strategy (only if Phase 1 passes)

- `CapitulationReversionConfig(StrategyConfig, frozen=True)` — instrument ids, bar types, the six factor params, scale/exit/risk params.
- `CapitulationReversionStrategy(RiskAwareStrategy, Strategy)` — RiskAware FIRST in the base tuple (MRO / node-side halt gate, per `ema_cross.py` convention). Subscribe SPY + ES bars; compute factors on daily bars; `on_bar` routes by instrument; scaled entries; `on_stop` flatten; `on_save`/`on_load` for restart continuity.
- Backtest with a realistic **FillModel** (slippage — nautilus gotcha #14); walk-forward / OOS split for the fitted thresholds; cost-sensitivity curve (does edge survive 1-2 bps?).
- Evaluate vs buy-and-hold on CAGR, Sharpe, max DD, time-in-market.

### Open questions for the plan phase

- VIX / VIX3M / McClellan ingestion path (do we have these series, or derive VIX term structure from VX futures?).
- ES continuous-contract roll handling for the volume z-score (registry alias windowing).
- SPY equity bar history depth available below the 2023-03-28 EQUS.MINI floor (historical dataset vs live feed).

---

## 7. Citations

- Lehmann (1990), _Fads, Martingales, and Market Efficiency_, QJE — weekly index reversal.
- Campbell, Grossman, Wang (1993), _Trading Volume and Serial Correlation in Stock Returns_ — high-volume declines revert (liquidity).
- Budish, Cramton, Shim (2015), _The High-Frequency Trading Arms Race_ — ES-SPY lead-lag compression (~97ms→~7ms), ES initiates 88.6% of arbs.
- Cont, Cucuringu, Zhang (2023), _Cross-Impact of Order Flow Imbalance in Equity Markets_, Quant Finance — lagged cross-asset OFI predicts ~1-min horizons.
- McLean, Pontiff (2016), _Does Academic Research Destroy Stock Return Predictability?_, JF — ~58% post-publication anomaly decay.
- Collin-Dufresne, Daniel — ~90% of large single-stock shocks permanent (reversal asymmetry index vs single name).
- Faber (2007), _A Quantitative Approach to Tactical Asset Allocation_ — MA timing reduces vol/DD.
- Andersen, Bondarenko — VPIN/order-flow toxicity adds no incremental predictive power after controls.
- Cboe — VIX / VIX3M term structure definition; backwardation as oversold/timing signal.

> Engine-diversity note: both the Claude and Codex passes independently converged on the same verdict (capitulation-reversion is a real but small/skewed liquidity-provision edge; daily bars + ES volume + VIX suffice; order-flow is execution-confirmation, not core edge). Codex supplied the primary-source citations above.
