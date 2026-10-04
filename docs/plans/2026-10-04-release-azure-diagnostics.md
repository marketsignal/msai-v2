# Preserve useful Azure failure diagnostics

## Scope and immutable base

Base: `23db3b838faaed1ab6dddb0d5c0f196f5e0e440d` (merged main). Worktree:
`fix/release-azure-diagnostics`. This repair makes deployment failures diagnosable;
it does not claim to repair the still-unknown GitHub runner Azure write failure.

## Reproduction and proven root cause

Normal deployment runs [37164968762](https://github.com/marketsignal/msai-v2/actions/runs/37164968762)
and [37165202036](https://github.com/marketsignal/msai-v2/actions/runs/37165202036)
passed revision/CI/auth/readiness gates but failed opening transient SSH access.
The former failed Azure delete, the latter Azure create, both exit 1. VM staging
and installation were skipped; existing application services remain at 23db3b8.
Operator-owned recovery removed the old completed-owner rule separately. That is
not evidence the runner identity can mutate the NSG.

`scripts/nsg_rule_lifecycle.py:az_json` captures subprocess stderr and discards it
on nonzero exit. A local subprocess mock with `ERROR: (AuthorizationFailed)` or
an `AttributeError` traceback yields the same generic refusal. Existing lifecycle
tests replace `az_json`, so do not exercise this wrapper. Independent inspection
found no proved command-argument defect. No IAM change is justified yet.

## Research and choice

The current [Azure CLI command reference](https://learn.microsoft.com/en-us/cli/azure/network/nsg/rule?view=azure-cli-latest)
matches the helper's create/delete flags. [Microsoft troubleshooting guidance](https://learn.microsoft.com/en-us/cli/azure/use-azure-cli-successfully-troubleshooting?view=azure-cli-latest)
distinguishes argument, authentication, resource and runtime/transport failures.
Adding raw stderr or debug logging could disclose credential-bearing diagnostic
text. Use one small formatter that emits symbolic codes or fixed categories,
never arbitrary message text. No new dependency or generic logging framework.

## Minimal change and paths

- `scripts/nsg_rule_lifecycle.py`: append safe detail to the existing nonzero-exit
  refusal. Extract a bounded symbolic code only from an anchored `ERROR: (Code)`
  or complete `Code: Code` line (letters/digits, starts with a letter, max 64
  characters). Otherwise return fixed labels for CLI argument errors, missing
  login, subscription selection failure, connection/TLS failure or a Python
  traceback. Unrecognized output explicitly says unclassified/details withheld.
  Do not echo message text, command arguments, raw stderr/stdout, environment,
  tokens or traceback bodies. Preserve all lifecycle decisions and subprocess
  arguments, successful JSON handling, failure exit status and timeout behavior.
- `tests/infra/test_nsg_rule_lifecycle.py`: separate subprocess-boundary tests;
  do not replace `az_json` in them. Observe RED before the production change.
  Cover create/delete/list standard codes, CLI and runtime categories, unknown
  and empty output, multiline sensitive payload suppression, length/character
  limits, successful JSON/empty delete output, malformed JSON and timeout.
  Existing lifecycle tests remain the control for ownership and mutation order.
- `docs/operations/release-safety.md`: how to interpret the bounded diagnostic;
  unknown output remains inconclusive. No automatic permission changes/retries.
- `docs/CHANGELOG.md`, this plan and a graduated operations use case: record the
  limited repair and actual evidence; do not claim normal deployment passed.

## Acceptance and verification

1. Known symbolic Azure failures expose their code without their message body.
2. Non-ARM CLI failures expose a fixed category or honest unclassified fallback.
3. All nonzero exits still refuse; no additional Azure/GitHub call, retry, access
   change or mutation follows from classification. Existing lifecycle controls pass.
4. Focused infra tests and applicable lint/type checks pass. No unrelated exhaustive
   backend/frontend test run is needed for this helper-only repair.
5. Real read-only CLI use case below passes. Actual GitHub write and deployment
   acceptance remain separate PARTIAL gates until the reviewed repair is published
   and exercised under the runner identity. Unit mocks are not cloud evidence.

## User journey and surface decision

CLI is covered through the documented operator helper. Product API/UI are N/A:
Azure deployment diagnostics are exposed only to the release operator, through
the helper and GitHub logs; the research/trading API and browser cannot invoke
or inspect this NSG helper. No application UI behavior changes.

### Operator diagnoses a failed NSG inspection and checks the correct target

- **Actor:** Release operator inspecting the existing MarketSignal Azure NSG.
- **Scenario:** An incorrect NSG name causes a read-only preview to fail. The
  operator needs a useful reason, then verifies the intended target safely.
- **Interface:** CLI.
- **Intent:** Understand the failed inspection and confirm the intended network
  rules are preserved before attempting another deployment.
- **Setup:** Existing sanctioned Azure/GitHub CLI login, explicitly selected
  MarketSignal2 subscription. No new identity or rule. Use `reap --dry-run` only.
- **Steps:** Invoke the helper preview with a deliberately nonexistent NSG name
  in the existing resource group; inspect stderr. Invoke it again with `msai-nsg`
  and the same explicit subscription/repository; inspect preservation decisions.
- **Verification:** Stderr explains the first refusal with the Azure resource
  error code and exit 1, without its free-form diagnostic. The corrected command
  reports the real rule names and read-only preservation/preview decisions. No
  `NSG_CREATED` or `NSG_ABSENT` deletion claim is produced by either preview.
- **Persistence:** Reinvoke the preview of the correct NSG and observe the same
  rule inventory/decisions; read-only commands must not change the network rules.

## Next operational gate

After review and authorized publication, run the normal pipeline under its OIDC
identity. Use the newly visible category/code to investigate the actual write
failure. Preserve prior PARTIAL reports. Only then complete deployment, active
owner protection, cleanup rerun and cancellation/failure recovery acceptance.
