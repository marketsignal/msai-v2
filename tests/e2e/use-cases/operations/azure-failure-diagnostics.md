# Azure inspection failure diagnostics

Graduated from the real read-only operator journey on October 4, 2026. Local
failure categories and create/delete propagation also have focused subprocess
tests; this journey does not certify GitHub runner writes or deployment.

## Surface coverage decision

- CLI: Covered through the documented `nsg_rule_lifecycle.py` operator helper.
- API: N/A — the product API does not invoke or expose Azure NSG diagnostics.
- UI: N/A — the application browser does not invoke or expose this release
  administration capability; operators receive these diagnostics in CLI/job logs.

## UC1: Diagnose a failed inspection and inspect the intended target

- **Actor:** Release operator inspecting the MarketSignal Azure environment.
- **Scenario:** A mistyped NSG name makes a read-only preview fail. The operator
  needs a useful reason and must check the intended target without changing rules.
- **Interface:** CLI.
- **Intent:** Understand the failed inspection and confirm the intended network
  rules remain preserved before deciding the next release action.
- **Setup:** Existing sanctioned Azure/GitHub CLI login; explicitly confirm
  MarketSignal2 subscription `68067b9b-943f-4461-8cb5-2bc97cbc462d` and operator
  `pablo@marketsignal.ai`. Do not change the CLI default, authenticate another
  identity, add rules, or invoke any mutating helper mode.
- **Steps:**
  1. Run `python3 scripts/nsg_rule_lifecycle.py reap --dry-run --repository
     marketsignal/msai-v2 --subscription 68067b9b-943f-4461-8cb5-2bc97cbc462d
     --resource-group msaiv2_rg --nsg-name msai-diagnostics-absent-20261004`.
     Inspect stderr and the exit status.
  2. Reinvoke with `--nsg-name msai-nsg`, all other arguments unchanged, and
     inspect the rule names and preservation/preview decisions.
- **Verification:** The failed preview's stderr explains `code=ResourceNotFound`
  with helper exit 1 and a nonzero Azure subprocess exit, without free-form
  provider diagnostics. The correct preview exits 0 and shows actual rules with
  `NSG_PRESERVED` or `NSG_WOULD_DELETE` decisions. Neither invocation reports
  `NSG_CREATED` or `NSG_ABSENT` deletion success. The October 4 baseline contained
  AllowHttpsInbound, AllowHttpInbound, AllowSshFromOperator and
  claude-hvp-session-ssh, all preserved; refresh the baseline for later runs.
- **Persistence:** Reinvoke the correct-target preview and confirm the same
  rule inventory/decisions through CLI output. If another authorized operator
  changes rules concurrently, record that interference rather than claiming
  persistence or altering their rules.
