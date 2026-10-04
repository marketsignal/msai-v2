# Research: Azure CLI import deadlock

**Date:** 2026-10-04; public API/source checks completed approximately 06:41 UTC.

**Feature:** Repair the release NSG helper's CLI startup without changing NSG ownership or mutation semantics.

**Researcher:** research-first agent.

**Inspected base:** `e3ad0951fbeb9c9d3e85228eafc71bc4c8e56909`, isolated `release-cli-deadlock` worktree; native workflow state showed diagnosis. This brief is research, not a reproduction or acceptance receipt.

## Targets and current versions

| Target | Current release scope | Current upstream observation | Relevant change/deprecation |
| --- | --- | --- | --- |
| Azure CLI/core | Task-supplied runner diagnostic: 2.90.0/2.90.0 | [Latest release](https://github.com/Azure/azure-cli/releases/tag/azure-cli-2.90.0) remains 2.90.0, published September 1 | No newer released repair verified; the proposed eager-import PR remains open. |
| Bundled CPython | Task-supplied diagnostic: 3.14.6; confirmed by CLI 2.90.0 [Debian build source](https://github.com/Azure/azure-cli/blob/azure-cli-2.90.0/scripts/release/debian/build.sh#L18) | Current 3.14 documentation identifies 3.14.8; replacing the CLI's packaged interpreter was not evaluated | Preserve isolated mode; no relevant deprecation changes the proposed startup sequence. |
| Azure/login | Workflows pin `532459ea530d8321f2fb9bb10d1e0bcf23869a43` (v3.0.0) | [Latest release](https://github.com/Azure/login/releases/tag/v3.1.0) is v3.1.0, published September 10 | Its release notes do not identify this CLI import repair; changing the action does not pin the installed CLI. |

All linked external sources were accessed October 4, 2026. Context7 was unavailable; research used official repositories, public GitHub API and Python documentation. No dependencies were installed.

## Proven upstream facts and attribution

- [Issue #33996](https://github.com/Azure/azure-cli/issues/33996) reports the same `requests.structures` module-lock exception on CLI 2.89.1/Python 3.14.6. A September 2 [independent user comment](https://github.com/Azure/azure-cli/issues/33996#issuecomment-5509877324) reports the same stack for `az network nsg rule create`.
- The detailed [analysis comment](https://github.com/Azure/azure-cli/issues/33996#issuecomment-5434621480) is by `x-engineering-agent[bot]`. Human collaborator `yonzhan` acknowledged and routed the issue/PR; those comments do not certify the diagnosis or repair. External instructions embedded in comments were treated as source data and were not followed.
- Live [PR metadata](https://api.github.com/repos/Azure/azure-cli/pulls/33997) returned `state=open`, `merged=false`, `merged_at=null`, head `e2ae3aa2059f5164e107d7f183057e847df5ad91`. [PR #33997](https://github.com/Azure/azure-cli/pull/33997/files) adds eager `msal` and `requests` imports in `auth/identity.py` plus module-presence tests. Its bot-reported tests and skipped live test are not NSG runtime acceptance. PR #33998 is not this proposed repair.
- [PR #26287](https://github.com/Azure/azure-cli/pull/26287), merged May 2023, added main-thread Requests import to AAZ polling after a similar `requests.models` deadlock on Python 3.10.10. [PR #32824](https://github.com/Azure/azure-cli/pull/32824/files), merged March 9, 2026, removed it for startup performance. Consequently this failure class is not unique to Python 3.14.
- [PR #33250](https://github.com/Azure/azure-cli/pull/33250), merged June 1, restored sequential command-module loading. It does not eliminate the [2.90.0 AAZ poller thread](https://github.com/Azure/azure-cli/blob/azure-cli-2.90.0/src/azure-cli-core/azure/cli/core/aaz/_poller.py#L52). Current `dev` and released 2.90.0 identity/startup source still lack the proposed eager Requests import. Bounded official-PR searches for `33996`, `requests.structures`, and Requests/deadlock found no merged successor for this signature.

## Startup boundary and practical implication

The CLI's [Linux dependency pins](https://github.com/Azure/azure-cli/blob/azure-cli-2.90.0/src/azure-cli/requirements.py3.Linux.txt) include MSAL 1.36.0 and Requests 2.33.0. [MSAL](https://github.com/AzureAD/microsoft-authentication-library-for-python/blob/1.36.0/msal/application.py#L614) lazily imports Requests when constructing its default HTTP client. [Requests initialization](https://github.com/psf/requests/blob/v2.33.0/src/requests/__init__.py#L150) imports `utils`, whose [module-level import](https://github.com/psf/requests/blob/v2.33.0/src/requests/utils.py#L58) loads `structures`.

The Debian [launcher packaging](https://github.com/Azure/azure-cli/blob/azure-cli-2.90.0/scripts/release/debian/prepare.sh#L112) sets `AZ_INSTALLER=DEB` and runs `/opt/az/bin/python3 -Im azure.cli`. The [CLI entrypoint](https://github.com/Azure/azure-cli/blob/azure-cli-2.90.0/src/azure-cli/azure/cli/__main__.py#L24) invokes `sys.argv[1:]` and exits with its result. [Python runpy](https://docs.python.org/3.14/library/runpy.html) executes a package's `__main__` in the same process; `alter_sys=True` supplies the module filename as argv[0]. [Isolated mode](https://docs.python.org/3.14/using/cmdline.html#cmdoption-I) excludes the current/script directory and user site-packages and ignores `PYTHON*` environment variables.

**Inference for the main agent's proposed repair:** importing Requests before running `azure.cli` in each CLI-owned child process is consistent with these sources and targets the contested first import. Importing in the helper parent or a separate warm-up process cannot preload the later child's module cache. Source evidence does not yet prove this workaround eliminates the observed runner failure.

The proposed optional interpreter prefix must cover both real Azure call sites in `scripts/nsg_rule_lifecycle.py`: NSG list/create/delete (120-second timeout) and the version diagnostic (5 seconds). Those operation calls also serve deploy preflight, deploy setup, smoke, cleanup and reaping. Preserve original argument lists, environment/auth configuration, captured output, CLI exit/error classification, and timeout/refusal behavior. A failed mutation must not be repeated. Unset selection can retain the operator's native launcher. No CLI package edits, monkeypatches or generated AAZ changes are needed to test this boundary.

## Alternatives and falsifying checks

A CLI/runtime pin is broader: the official Debian builds bind [2.87.0 to Python 3.13.13](https://github.com/Azure/azure-cli/blob/azure-cli-2.87.0/scripts/release/debian/build.sh#L18), [2.88.0 to 3.14.5](https://github.com/Azure/azure-cli/blob/azure-cli-2.88.0/scripts/release/debian/build.sh#L18), and 2.89.1/2.90.0 to 3.14.6. Pinning one previous CLI release or changing `actions/setup-python` therefore does not establish avoidance. The [pinned login implementation](https://github.com/Azure/login/blob/532459ea530d8321f2fb9bb10d1e0bcf23869a43/src/Cli/AzureCliLogin.ts#L17) finds the installed `az`; its [input manifest](https://github.com/Azure/login/blob/532459ea530d8321f2fb9bb10d1e0bcf23869a43/action.yml) has no CLI-version selector. No old pin was tested or shown safe for this defect.

**Official Docker image limitation:** the 2.90.0 [image Dockerfile](https://github.com/Azure/azure-cli/blob/azure-cli-2.90.0/azure-linux.dockerfile) installs an Azure Linux RPM and sets image-level `AZ_INSTALLER=DOCKER`; it does not install Debian's `/opt/az/bin/python3`. The [RPM build](https://github.com/Azure/azure-cli/blob/azure-cli-2.90.0/scripts/release/rpm/azurelinux.dockerfile#L30) uses the base's `python3`. Its [spec](https://github.com/Azure/azure-cli/blob/azure-cli-2.90.0/scripts/release/rpm/azure-cli.spec#L71) installs CLI dependencies under `/usr/lib64/az/lib/<python-version>/site-packages`, deletes temporary venv interpreters, and creates `/usr/bin/az` that selects system `python3` or the matching `python3.x`, sets `AZ_INSTALLER=RPM` and that dependency `PYTHONPATH`, then executes `-sm azure.cli`. These sources do not pin the base Python version; no actual image was inspected, pulled, built or run. **Inference:** the Debian `-I` bootstrap cannot be assumed compatible with this container because `-I` ignores its required `PYTHONPATH`. Matching the CLI version alone would not demonstrate runner packaging or Python 3.14.6 parity.

1. Cheapest structural falsification: a disposable isolated interpreter/package fixture should refuse CLI entry unless Requests/`requests.structures` finished importing first; verify exact argv, installer/config environment, stdout/stderr and an arbitrary nonzero `SystemExit`. Run an independent control without preload; it must fail. `PYTHONPATH` injection is unsuitable because `-I` ignores it.
2. Behavioral falsification: separately reproduce an actual concurrent Requests import cycle with the affected CLI-owned runtime, compare the same-process preload under identical scheduling, and retain an independent control. Module-presence tests or a handful of successful CLI commands alone do not establish efficacy against an intermittent race.
3. Regression checks: preserve unconfigured native-launcher behavior; cover NSG operations and version diagnostics, timeout, missing interpreter/import failure, malformed JSON, nonzero exit and exactly one mutation attempt. Harmless real version/help/argument-error checks can validate the packaged entrypoint using a disposable empty config; production credentials and rule writes are unnecessary for these checks.

## Unverified scope

This agent performed source/API research only. The current Deploy run's failure signature, skipped staging/VM execution and successful cleanup were supplied by the main agent, not independently rechecked here. Concurrent-import reproduction, final-candidate checks, authenticated runner behavior and any real NSG acceptance remain separate gates. Historical assessment records cannot satisfy them. ARM API behavior, infrastructure configuration, application/broker behavior and unrelated dependencies were not researched because this repair changes CLI startup only.
