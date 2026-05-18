import { notFound } from "next/navigation";

/**
 * Live-Trading Portfolio Compose — HARD-DISABLED in the ui-completeness PR.
 *
 * Council verdict 2026-05-17 (5/5 advisors APPROVE B with carve-out;
 * Codex research recommends B; full transcript persisted in
 * `docs/decisions/2026-05-17-portfolio-backtest-deferred.md`):
 *
 * The original portfolio-compose UX required users to hand-write per-strategy
 * Config (JSON), comma-separated instruments, and a fixed 0–1 weight. Pablo
 * rejected this UX during the iter-3 walkthrough — quote: "users don't need
 * to deal with json, they select the strategies to go in the portfolio, then
 * at portfolio level they pick the risk, allocation methods to each strategy,
 * then the user should be able to backtest the portfolio to see how it would
 * behave in the past."
 *
 * Industry research (Composer, QuantConnect, Build Alpha, RealTest, AlgoTest,
 * López de Prado HRP, Carver Systematic Trading) confirms: dominant pattern is
 * a form-based multi-select compose + allocation method picker (equal-weight,
 * inverse-vol, vol-targeted, risk-parity, HRP) + per-strategy risk policy +
 * portfolio-level backtest with combined equity curve + correlation matrix +
 * drawdown breakdown. MSAI's current UX is far below that bar.
 *
 * Rather than ship a deprecated UX users would have to unlearn, this route
 * returns 404 so direct URL access fails alongside the removed sidebar/nav
 * link. The redesign is queued as a dedicated `/new-feature portfolio-
 * backtest` PR with its own PRD/research/council/plan cycle.
 *
 * To re-enable: delete this guard and restore the prior `PortfolioCompose` /
 * `PortfolioStartDialog` wiring from git history (the original code is
 * preserved on the parent commit). DO NOT do this without first landing the
 * redesign — see the deferral doc for the new contract.
 */
export default function LivePortfolioDeprecatedPage(): never {
  notFound();
}
