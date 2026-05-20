import { redirect } from "next/navigation";

/**
 * Live-Trading Portfolio Compose — REDIRECTED to the new /portfolio composer.
 *
 * Per `docs/decisions/2026-05-17-portfolio-backtest-deferred.md` follow-up
 * (Task H8 of `docs/plans/portfolio-backtest.md`): the form-based composer
 * at /portfolio/new supersedes the rejected JSON-based live compose. Users
 * compose + backtest a portfolio there, then click "Deploy as Live
 * Portfolio" on the results page to promote it to live.
 *
 * Council verdict 2026-05-17 (5/5 advisors APPROVE B with carve-out;
 * full transcript in the decision doc above) found the JSON-config UX
 * unfixable in place; this redirect ensures direct URL access flows to
 * the new composer alongside the removed sidebar/nav link.
 */
export default function Page(): never {
  redirect("/portfolio/new");
}
