# Ecosystem and audit baseline

Assessed on 2026-10-03 against code commit `7f9eb2b5ad252bc3ba730f53a159ca04046278eb`. This is an assessment of the existing root implementation. No dependencies, broker configuration, application code, infrastructure, or databases were changed.

## Repository and runtime evidence

The initial checkout was clean on `main`. The latest eight commits include the October 2 strategy-path correction and gateway watchdog, followed by Forge upgrades. The conversation's historical `codex-version/` test counts describe a different implementation. The April 19 decision retained the former Claude implementation, flattened it to the root, and archived the alternative; see `docs/decisions/which-version-to-keep.md:5` and `docs/agent-context.md:15`.

The machine-readable [inventory](inventory.json) was generated from `git ls-files`, physical line counts, and Python ASTs, without importing application code:

| Area | Tracked files | Physical lines including comments and blanks |
| --- | ---: | ---: |
| Backend Python source | 224 | 70,331 |
| Backend Python tests | 357 | 106,073 |
| Frontend TS, TSX and CSS source | 137 | 24,147 |
| Strategy Python files | 8 | 723 |
| Existing Markdown documentation | 206 | 111,018 |

There are 24 frontend `page.tsx` files, seven browser spec files, and 48 migration files. AST inventory counted 2,582 unit, 836 integration, and nine E2E test functions. These are **source inventory counts**, not passing tests, coverage percentages, or collected parametrized cases. New audit documents are excluded because they were untracked during inventory.

Read-only runtime checks:

- `docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'`: repeated outside the filesystem sandbox after socket denial; succeeded and showed no running MSAI containers. Two unrelated containers were running and were untouched.
- `curl -sS --max-time 5 http://localhost:8800/health`: repeated outside the sandbox; connection refused, exit 7. This establishes a stopped/unavailable local endpoint, not a server-side code defect.
- Azure, remote production containers, current database state, data holdings, Entra login, vendor entitlement and broker connectivity were not checked.

## Dependencies and currency

The selected backend lock versions matched installed package metadata. The frontend dependency directory existed but was empty, as confirmed by the product/operations agent. Declared package versions are not proof of the current Azure deployment.

| Component | Repository version | Current primary-source evidence | Assessment |
| --- | --- | --- | --- |
| NautilusTrader | 1.223.0, `backend/uv.lock:1471` | Upstream lists 1.231.0, released August 2, and 2.0.0rc5, released September 15. The 1.231.0 notes describe the v1 to v2 transition. | Behind upstream; evaluate adapter fixes on an isolated compatibility branch. A release candidate is not an automatic production upgrade. [Releases](https://github.com/nautechsystems/nautilus_trader/releases) |
| Next.js | 15.5.12, `frontend/package.json:22`; matching ESLint config at line 44 | September 30 security release ships 15.5.27 and 16.3.8. Version 15 is Maintenance LTS; version 16 is Active LTS. | Security maintenance is overdue. Patch within the supported line first; a major rewrite is unnecessary. [Security release](https://nextjs.org/blog/september-2026-security-release), [support policy](https://nextjs.org/support-policy) |
| FastAPI | 0.133.1, `backend/uv.lock:643` | Official releases show 0.142.2 on September 30. | Not latest; upgrade through API/auth regression checks, rather than treating an older version alone as a defect. [Releases](https://github.com/fastapi/fastapi/releases) |
| Databento Python | 0.71.0, `backend/uv.lock:523` | Official changelog lists 0.87.0 on September 22. | Adapter/DBN compatibility review is warranted. No dataset subscriptions or historical coverage were re-audited. [Changelog](https://github.com/databento/databento-python/blob/main/CHANGELOG.md) |
| arq | 0.27.0, `backend/uv.lock:136` | PyPI lists 0.28.0 and explicitly describes the project as maintenance-only. | Keep while restoring correctness; document ownership and an eventual exit criterion. Maintenance mode alone does not justify replacing a working queue. [Maintainer package page](https://pypi.org/project/arq/) |
| DuckDB | 1.4.4, `backend/uv.lock:586` | The 1.4 LTS announcement gives September 16, 2026 as end of community support, while the current FAQ still lists 1.4 as supported and 1.5 as current. | Official pages conflict on current support; resolve before choosing an upgrade target. Do not label 1.4.4 current or unsupported without qualification. [LTS announcement](https://duckdb.org/2025/09/16/announcing-duckdb-140), [FAQ](https://duckdb.org/faq) |
| PostgreSQL | `postgres:16-alpine`, `docker-compose.prod.yml:61` | Version 16 remains supported through November 9, 2028. | Appropriate supported major; deployed minor/image digest not inspected. [Versioning policy](https://www.postgresql.org/support/versioning/) |
| Remaining dependencies | React 19.1.0, QuantStats 0.0.81, pandas 2.3.3, PyArrow 23.0.1 and others are inventoried | Not individually release/advisory-certified in this bounded audit. | A complete dependency vulnerability scan remains a phase-two task. |

**Security scope:** the patch lag is verified, but exploitation is not. Source inspection found an App Router application, a standalone build, no configured `images.remotePatterns`, no `next/image` imports, no middleware, and no rewrites. Several September advisories require features absent from this tree. The August Windows-specific issue does not describe this Linux deployment. This does not certify all transitive dependencies or all advisories as safe. See [product/operations evidence](product-operations.md) and the [August release](https://nextjs.org/blog/august-2026-security-release).

## Comparison with practical alternatives

These are engineering judgments informed by primary product documentation, not comparative performance tests or purchasing recommendations. No costs, vendor storage estimates, or strategy-return claims were reused from the old plan.

| Option | What primary documentation establishes | Implication for MSAI |
| --- | --- | --- |
| MSAI using NautilusTrader | Nautilus already provides a common event-driven architecture for simulation and live execution, with Python strategy logic. [Documentation](https://nautilustrader.io/docs/latest/) | The core engine choice fits. MSAI's distinct value is its operator workflow, account governance and interfaces. It should consume engine contracts rather than recreate order/account semantics. |
| QuantConnect / LEAN | LEAN supports Python/C# research, backtesting and live trading. The CLI supports local and cloud IB deployment; the documented CLI workflow requires a paid-tier organization. [Engine](https://www.quantconnect.com/docs/v2/lean-engine/getting-started), [IB deployment](https://www.quantconnect.com/docs/v2/lean-cli/live-trading/brokerages/interactive-brokers) | A credible benchmark and possible buy-versus-build alternative. MSAI must justify its ongoing maintenance through custom Azure/Entra/account workflow needs. No evidence here proves MSAI has better returns, reliability, speed or total cost. |
| Thin Nautilus service plus notebooks/CLI | Nautilus exposes backtesting and live trading directly. [Documentation](https://nautilustrader.io/docs/latest/) | Inference: fewer platform components would likely reduce ownership for one researcher, at the expense of the custom multi-user dashboard and operational controls. A useful simplification baseline, not a proposal to discard the current system. |
| VectorBT research | Its Portfolio API provides optimized portfolio simulation and performance analysis, including costs. [Portfolio API](https://vectorbt.dev/api/portfolio/base/) | Suitable comparison for research iteration speed. It does not, by that fact alone, replace MSAI's specific IB execution, account and recovery workflow. Benchmark identical inputs and fee assumptions before adopting a second engine. |

MSAI has **potentially valuable custom integration**, not a demonstrated moat or investment edge. The repository contains much more platform machinery than strategy content. A controlled benchmark and one trustworthy complete workflow are stronger next evidence than another feature inventory.

## Efficiency evidence and limits

The division between PostgreSQL application state, Parquet historical bars, DuckDB queries and Redis coordination is a sensible small-platform design. DuckDB reads Parquet in-process; another analytics database is not justified by this audit. [DuckDB project](https://pypi.org/project/duckdb/)

Compose source defines 12 service entries in each environment, including optional brokers and, in production, a one-shot migration service. This is not a claim that 12 services currently run. Production resource ceilings sum to approximately 17 CPUs and 32 GiB with the broker profile; ceilings are not reservations or measured use. Development research/backtest/portfolio/ingest worker definitions have no explicit CPU/memory limits. Evidence: `docker-compose.dev.yml:148`, `docker-compose.prod.yml:61` through `:630`.

No throughput, CPU, memory, 100-symbol workload, options-chain scale, recovery-duration or cost benchmark was run. Efficiency is **unmeasured overall**. Localized scaling concerns and correctness probes are documented by the subsystem agents. The earlier design's storage and vendor-cost estimates remain unsuitable for sizing or purchasing.
