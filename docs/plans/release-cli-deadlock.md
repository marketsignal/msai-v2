# Azure CLI startup repair for release network operations

Status: diagnosis/design draft; no implementation or cloud action authorized by
this document. Immutable workflow base: `e3ad0951fbeb9c9d3e85228eafc71bc4c8e56909`.

## Problem and evidence

Main CI passed at this revision: 3,748 backend tests, frontend/image-path, auth
and image builds. Automatic Deploy37182097303 nevertheless failed at NSG create
with the exact captured `REQUESTS_STRUCTURES_IMPORT_DEADLOCK` signature,
CLI/core2.90.0 and bundled Python3.14.6. OIDC and release gates passed; both staging
steps and VM execution were skipped. Independent cleanup passed and the
14-field before/after inventory preserved all four unrelated rules.
This identifies this attempt's import-lock failure, not earlier generic failures
or whether an ARM write preceded the exception.

Research: [dated primary-source brief](../research/2026-10-04-azure-cli-import-deadlock.md).
Azure issue33996 reports the matching failure, including NSG creation. Its actual
proposed fix33997 is still open; the current latest CLI release is2.90.0.
Bot analysis is a hypothesis, not maintainer certification. A separate fresh
investigation and independent reproduction must verify the executable startup
ordering and controls before treating the proposed repair as actionable.

## Minimal repair

The CLI's lazy requests import must finish in its own main process before Azure
command/poller threads run. Parent imports or a separate warm-up process cannot
populate the child process's module cache. Installing a different runner system
Python does not change the CLI's bundled Python. A version downgrade requires a
separately validated runtime; no dependency downgrade or mutation retry is added.

1. Add a small `scripts/azure_cli_startup.py` entrypoint that imports requests,
   then runs the unmodified `azure.cli` module as `__main__` with runpy and
   `alter_sys=True`. Preserve argv, inherited auth/config environment, stdout,
   stderr and SystemExit; no exception suppression or retry.
2. In the NSG helper, an explicitly configured `MSAI_AZURE_CLI_PYTHON` selects
   that CLI-owned interpreter with `-I` and the sibling entrypoint. An unset
   value retains the operator's native az launcher. Both network operations and
   the existing five-second version diagnostic use the same configured prefix.
   Preserve the existing120-second operation limit, classification/redaction,
   original failed-operation result and no-fallback/no-retry behavior.
3. The three NSG-owning workflows explicitly supply
   `MSAI_AZURE_CLI_PYTHON=/opt/az/bin/python3` and `AZ_INSTALLER=DEB`, matching
   the GitHub Ubuntu runner's official Debian launcher. Cover all seven caller
   paths: deploy/preflight create and cleanup, smoke create and cleanup, reaper.
   Keep revision/fleet/ownership guards, cancellation, SSH bounds, grants and
   all targets unchanged. Do not patch installed vendor packages.

## Ownership and changed paths

Producer owns the new startup entrypoint, `scripts/nsg_rule_lifecycle.py`,
`.github/workflows/{deploy,smoke,reap-orphan-nsg-rules}.yml` and the owning
`tests/infra/test_nsg_rule_lifecycle.py`/`test_release_safety.py` tests.
Keep new startup controls in the already executed owning tests rather than adding
an unrelated CI platform. Main owns this plan, operational/context/master
documentation and changelog. Research-first owns the dated source brief.
No backend, frontend, strategy, schema, broker or Forge harness implementation.

## TDD and focused verification

Before production edits, execute RED controls for actual child startup ordering,
the configured helper prefix and all workflow caller paths. Use a disposable
isolated venv/package fixture: `-I` intentionally ignores PYTHONPATH and prevents
the working directory from shadowing installed requests/azure modules.
The Azure provider boundary may be fake; run the real helper and entrypoint.

Require requests.structures loaded on the main thread before the CLI entrypoint
starts, exact argument/environment propagation, isolation, truthful nonzero exit,
and no operation retry/native fallback after configured launch/import failure.
Cover the version diagnostic's same runtime and existing redaction/time bounds.
Keep existing native-launcher and ownership/fleet/cancellation controls passing.
Run owning unittest suites, existing deployment shell contract/Actionlint,
scoped Python lint/typing and whitespace checks. No exhaustive local app suite.

## User journey and acceptance boundary

#### E2E Use Cases

### UC-CLI-STARTUP-001: Recover a safe access-policy preview

Actor: MarketSignal release operator with the existing authorized Azure login.

Scenario: The operator wants to preview cleanup decisions before a release and
has selected an unavailable network-policy target. They need a clear refusal
and a successful preview after correcting the target, without changing policy.

Intent: Inspect deployment-access policy reliably and understand a refusal
without exposing credentials.

Interface: CLI.

Setup: Use the existing pablo@marketsignal.ai Azure authentication, explicitly
select subscription `68067b9b-943f-4461-8cb5-2bc97cbc462d`, RG `msaiv2_rg`, and
the CLI-owned interpreter. Local Homebrew mode sets `AZ_INSTALLER=HOMEBREW`;
the workflow's Debian mode sets `AZ_INSTALLER=DEB`. Capture the actual NSG's
14-field rule inventory through Azure CLI. No rule seed or direct state injection.

Steps:

1. Invoke the configured NSG helper's `reap --dry-run` with the unavailable
   target `msai-cli-startup-absent-20261004` and the explicit subscription/RG.
2. Correct the target to `msai-nsg` and invoke the same configured preview.
3. Repeat the correct-target preview and obtain a fresh Azure CLI inventory.

Verification: stderr explains the missing-target refusal with a symbolic
resource error and no secret/body. Correct-target stdout shows all four
preserved unrelated rules and no create/delete marker. Both valid previews
agree; the before/after 14-field inventories match.

Persistence: The repeated preview and fresh inventory confirm the same policy
through public CLI interfaces; no cloud state is changed by this journey.

### Surface coverage decision

CLI: Covered by UC-CLI-STARTUP-001.

API: N/A — release-access policy is operated through the GitHub/Azure CLI,
not a product API; the application/fleet gate retains its owning regression.

UI: N/A — this release-operator journey has no dashboard control. No UI
implementation changes or unrelated browser certification are claimed.

Pre-publication acceptance covers the exact frozen code, executable isolated
startup controls and actual read-only authenticated CLI journey only. A successful
Mac CLI2.83/Python3.13 journey is functional compatibility evidence, not proof of
the failing Linux3.14 runtime. If a matching official packaged runtime can be
tested safely offline, record its exact version and results separately.
The official RPM Docker image cannot certify the Debian bundled interpreter
unchanged: it relies on a package PYTHONPATH that isolated mode excludes.

After separately authorized integration, exact-main CI and the real normal
deployment must pass with the configured runner startup. Then repeat RC-1:
ordinary cancellation during propagation before staging, producer cancelled,
staging/execution skipped, cleanup successful, repeated owned-rule absence and
unrelated-rule equality. Obtain genuine schedule-event reaper evidence separately.
Keep M20/full operational release acceptance PARTIAL until those criteria pass.
No workflow dispatch, NSG write, release, broker operation or policy expansion
is authorized by plan/code-review completion. Preserve all earlier failed runs.
