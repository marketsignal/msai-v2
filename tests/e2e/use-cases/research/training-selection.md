# Research training selection and exploratory discovery

Graduated after the real API, CLI and browser preliminary journeys and named layout/recovery closure passed on October 4, 2026. The approved plan remains unchanged. Final candidate verification is separate. These short existing-data cases establish workflow behavior, not alpha or production trading readiness.

## Surface coverage decision and user journeys

All three exposed capability surfaces are covered. API, CLI and UI are each required; no exclusion based on unchanged implementation paths. Scope: guarded local equity/minute research, existing licensed AAPL data and `example.ema_cross`; no downloads or broker calls/orders. Check actual source mounts and named disabled broker/vendor guards before restarting only quiet application services. The stable source override currently targets primary and must not be reused unchanged to claim candidate testing.

### UC-RSB-API — exploratory sweep and honest discovery

- Actor: Research operator submitting a small strategy experiment through the public API.
- Scenario: Existing AAPL data and a registered EMA strategy are available; the operator wants a training-selected configuration and a separately visible reserved-period diagnostic before storing it for further research.
- Intent: Run an experiment, understand how its choice was made, and retain a discovery candidate without mistaking it for validated alpha.
- Interface: API.
- Setup: Authenticate using the configured supported development key; refresh available strategy, canonical instrument/config and bar-window via public GETs. Reuse the previously working research job4f39f4ff-3c54-44e1-a190-b0123feba503 as an input reference, not proof for the new run. No raw DB/queue/Parquet arrange.
- Steps: POST a two-choice grid (fast EMA5/10, slow20, one-share size) over existing January22–25,2025 data with holdout_days2/purge0; poll job progress; inspect detail and trials; create a discovery candidate. Explicitly promote a different eligible trial using trial_index and inspect its manual-selection provenance. POST one bounded walk-forward experiment over the same available data with small training/test windows and purge0; inspect the latest training choice and separate test results, then create discovery or inspect its concrete refusal. Request legacy-job promotion and a failed/ineligible result through the supported route to observe refusal.
- Verification: Responses include training selection and distinct diagnostic metrics/state/dates; candidates are discovery with training metrics and automatic/manual provenance. Walk-forward retains the latest training winner and reports test failure independently. If a real empty/failing latest test cannot be induced through supported inputs, retain an explicit limitation and prove that failure control in the owning finalizer regression; the successful real walk-forward journey is still mandatory. Refusal explains rerun/eligibility. Counterfactual numerical invariants are proved separately by owning tests, not inferred from this market example.
- Persistence: Re-request job/candidate and list history; identity/config/selection/diagnostic/refusal remain consistent.

### UC-RSB-CLI — research and rerun guidance

- Actor: Research operator driving the same small experiment from the CLI.
- Scenario: The operator needs to submit and revisit a research run from a shell with the same meanings as the dashboard.
- Intent: Complete a research-to-discovery workflow and understand a legacy refusal without infrastructure knowledge.
- Interface: CLI.
- Setup: Intended local API URL and configured supported API key; same refreshed public input reference, no secret output.
- Steps: Submit via `research sweep --config @payload.json`; use list/show until terminal; create discovery using research promote; explicitly choose a different eligible trial using --trial-index. Submit a bounded walk-forward run via the existing walk-forward command, inspect its latest training selection and diagnostic tests, and create discovery or observe an evidence-based refusal. Invoke legacy promotion.
- Verification: stdout includes training selection contract and separately labeled diagnostic evidence for both sweep and walk-forward; discovery identities and explicit-trial provenance agree with API detail; refusal explains why a rerun is needed rather than exposing a stack trace or secret. A real failing latest test is desirable but not inferred if only the owning regression exercised it.
- Persistence: A separate show/list invocation retains the same experiment/config and candidate observations.

### UC-RSB-UI — understandable research outcome and recovery

- Actor: Research operator using the real local browser dashboard.
- Scenario: The operator wants to launch the small experiment, follow progress and retain an exploratory choice, then revisit it later.
- Intent: Complete research and see clearly what is selected, tested and still unvalidated.
- Interface: UI.
- Setup: Running candidate-mounted guarded app with supported development authentication; refresh real inputs via public interfaces. No response interception/mocks.
- Steps: Launch a grid through the form with bounded parameters, holdout_days2 and purge0; follow progress; open result; inspect separate training/diagnostic sections; create Discovery Candidate; navigate to its graduation view. Switch the form to walk-forward, launch a bounded run, inspect the latest training choice and separate test diagnostics, then create discovery or inspect its refusal. Open a legacy job and inspect rerun explanation. Submit a deliberately invalid strategy configuration, observe persisted failure/no eligible winner, correct it and launch a successful run. Confirm an invalid explicit holdout/purge combination is refused clearly.
- Verification: Plain wording, resolved dates and diagnostic status are readable; discovery creation and refusal reflect API eligibility; operator can identify the next action. Capture real computer-use evidence and appropriate loading/error/legacy states.
- Persistence: Reload research/candidate views; selection/evidence/candidate stage and failure state persist against real backend responses.

Repeatable UI replay: `frontend/tests/e2e/specs/research-selection-real.spec.ts`. Opt in with `MSAI_REAL_RESEARCH_E2E=1` and an explicit `PLAYWRIGHT_BASE_URL` only after inspecting the intended guarded existing-data research target. It uses the real backend, existing registered EMA strategy and licensed AAPL data; it neither fabricates responses nor prepares state through internal storage. The legacy job fixture defaults to the observed historical job above; another target must supply its genuine legacy job via `MSAI_RESEARCH_LEGACY_JOB_ID`. UI bypass does not certify Entra authentication. The separate controlled-response readability spec establishes isolated message/layout regressions.

