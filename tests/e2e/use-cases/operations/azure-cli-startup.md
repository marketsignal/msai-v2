# Azure CLI startup: safe release-access preview

Graduated after the October 4, 2026 preliminary feature journey. This covers
actual read-only CLI compatibility; it does not certify Linux runner deployment,
cancellation, scheduled recovery, or trading. See the
[startup assessment](../../../../docs/audits/2026-10-04/azure-cli-startup.md).

## UC-CLI-STARTUP-001: Recover a safe access-policy preview

Actor: MarketSignal release operator with the existing authorized Azure login.

Scenario: The operator wants to preview cleanup decisions before a release and
has selected an unavailable network-policy target. They need a clear refusal
and a successful preview after correcting the target, without changing policy.

Intent: Inspect deployment-access policy reliably and understand a refusal
without exposing credentials.

Interface: CLI.

Setup: Use existing pablo@marketsignal.ai Azure authentication. Explicitly select
subscription `68067b9b-943f-4461-8cb5-2bc97cbc462d`, RG `msaiv2_rg`, and the
CLI-owned interpreter via `MSAI_AZURE_CLI_PYTHON`. Local Homebrew mode sets
`AZ_INSTALLER=HOMEBREW`; the workflow's Debian mode sets `AZ_INSTALLER=DEB`.
Capture the NSG's 14-field rule inventory through public Azure CLI. No rule
seed, policy write or direct state injection is permitted.

Steps:

1. Invoke `scripts/nsg_rule_lifecycle.py reap --dry-run` with repository
   `marketsignal/msai-v2`, the explicit subscription/RG and unavailable target
   `msai-cli-startup-absent-20261004`.
2. Correct the target to `msai-nsg` and invoke the same configured preview.
3. Repeat the correct-target preview and obtain a fresh Azure CLI inventory.

Verification: stderr explains the missing-target refusal with a symbolic
resource error and no secret/body. Correct-target stdout shows four preserved
unrelated rules and no create/delete marker. Both valid previews agree; the
before/after 14-field inventories match.

Persistence: The repeated preview and fresh inventory confirm the same policy
through public CLI interfaces; no cloud state is changed by this journey.

## Surface coverage decision

CLI: UC-CLI-STARTUP-001 covers the release operator's actual preview journey.

API: N/A — release-access policy is operated through GitHub/Azure CLI, not a
product API. The application/fleet gate retains its owning regression.

UI: N/A — this release-operator journey has no dashboard control. There is no
UI implementation change or unrelated browser certification claim.

The owning offline specifications are `test_nsg_rule_lifecycle.py` and
`test_release_safety.py` under `tests/infra/`. They execute the real helper,
isolated startup and workflow blocks with disposable external-package/provider
doubles. Printed fixture versions are test data, not real runtime evidence.
