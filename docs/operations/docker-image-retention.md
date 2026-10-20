# Docker image retention

The reviewed source supplies a report-only CLI, a Mac per-user LaunchAgent installer and a VM deployment guard. Source tests do not install maintenance. Record separately which helper/config is installed, the daemon ID, approved scope, native scheduler status, before/after inventories and actual bytes recovered. Pipeline adoption requires publication/deployment; an existing VM does not acquire the guard from a local source edit.

## Policy and reports

Run `python3 scripts/docker-image-retention.py --config /absolute/path/config.json`. It prints JSON and deletes nothing by default. Config pins an absolute Docker executable, explicit context and expected daemon ID. Wrong daemon, invalid config, incomplete inventory or missing VM image authority refuse. Docker unavailable produces `SKIP_DOCKER_UNAVAILABLE` without starting Docker or any container (VM guard treats a skip as failure).

`groups` maps names to exact repository lists. One local group spans all enrolled historical iteration repositories and stable `msai-backend` / `msai-frontend` repositories. Build future local revisions under those stable names, for example `msai-backend:research-<revision>`; a new `msai-*` repository is reported under `unapproved_repositories` and preserved until explicitly enrolled after review. Ownership never follows a prefix. VM backend/frontend repositories have separate groups.

The selected policy keeps the current release plus one proven rollback alongside required infrastructure. New installer defaults explicitly set `keep_recent: 0`, with no arbitrary newest-version reserve or built-in historical VM release pins. VM current/last-good authorities identify the retained release refs; verify the rollback's accepted evidence rather than choosing it by build age. Those authorities may coincide after a rollback, so they do not guarantee two distinct releases. Local current/rollback refs require explicit pins; all existing V2 baseline/qualified/control pins remain while qualification is unfinished. Installed/operator-added pins are preserved on update and retire only through a deliberate reviewed config edit.

The seven-day **image creation** grace remains; it is not last-pull/last-use age. Every running or stopped container reference and prospective deployment pin also wins. The old supervisor's image stays protected while its container references it; maintenance does not recreate or align that service. Explicitly pin its image before any separately authorized recreation if it must remain available afterward. Required infrastructure and other unowned images, multiple tags, unknown/mixed ownership, unknown dangling/digest-only images and unknown/invalid/future creation dates are preserved. An image missing `Created` is individually reported as `unknown-created`; other valid records remain independently eligible. Explicit reviewed configs may select a nonzero `keep_recent`; the standalone CLI retains its omitted-key compatibility default of three. `grace_days` alternatives also require explicit config review. Re-run report-only after policy changes; a superseded candidate manifest does not approve new image IDs.

After exact operational approval, apply a report's full IDs:

```bash
python3 scripts/docker-image-retention.py --config /absolute/path/config.json \
  --apply --approve-id sha256:<64-lowercase-hex-digits>
```

Repeat `--approve-id` for each approved ID. `--apply --approve-policy` explicitly applies the configured recurring policy; use it only after approval of that policy/schedule or serialized deployment hook. It is not implied by running a report. The initial candidate list bounds each invocation. Before every removal the helper checks daemon identity, all container references, all aliases, config and deployment pins again. Removal uses only `docker --context <context> image rm --no-prune <full-ID>` without force. Native conflicts stop mutation. This is additional protection, not a transaction against arbitrary concurrent Docker clients.

No container, network, volume, builder cache, registry object or parent cleanup is performed. A local nonoverlap lock also covers manual invocation. The local process probe skips observed Docker builds/buildx builds/bakes, Compose build/pull/up and deploy scripts. These checks cannot prevent arbitrary concurrent clients; schedule outside builds/deployments. The default process probe is `/bin/ps`; failure to inspect processes refuses. `process_probe` is an optional absolute executable override for a reviewed host/fixture, never an instruction to suppress a real build.

JSON includes protected reasons, candidates, removed/skipped IDs, pins, before/after Docker disk usage and host filesystem available bytes. Docker layer sums are not promised recoverable space. On Mac, **host filesystem free space does not establish Docker Desktop internal disk-image free space**. Default floor is 5 GiB (`min_free_bytes`); insufficient available bytes produces `DISK_PRESSURE` / exit 3. Refusal exits 2. Successful report/apply and safe local skips exit 0. Retained images, unrelated workloads and volumes can still exhaust disk.

## Mac installation

Preview first, with an absolute Python >=3.10 path that survives worktree removal:

```bash
python3 scripts/install-image-retention.py --python /opt/homebrew/bin/python3
```

Preview prints the complete config and plist without writing them or loading launchd. Defaults name this operator's observed `desktop-linux` daemon ID and `$HOME/.docker/bin/docker`; verify both using `docker --context desktop-linux info --format '{{.ID}}'`. Supply a reviewed partial/full JSON file using `--config /absolute/path/overrides.json` to pin a different executable/context/engine. Installer updates union existing/default/new explicit pins and repository enrollments, preserving operator additions. Review that merged config before approval; edit the installed config deliberately to retire a pin.

After approval of the displayed recurring policy, run the same command with `--apply`. Installation runs a real report first and refuses to register on skip, refusal or low space. It does not apply retention immediately. It copies the helper/config into `$HOME/Library/Application Support/MSAI/image-retention/` and writes `$HOME/Library/LaunchAgents/ai.marketsignal.image-retention.plist`. Existing helper/config/plist are copied to a timestamped backup directory before replacement. The daily 03:15 local-time LaunchAgent uses absolute Python/helper/config paths, `--apply --approve-policy`, separate stdout/stderr logs and no run-at-load. It requires a logged-in user; sleep/offline timing is not uninterrupted maintenance.

```bash
python3 scripts/install-image-retention.py --status
launchctl print gui/$(id -u)/ai.marketsignal.image-retention
```

The installer verifies native `launchctl print` after bootstrap. Registration is not proof of a successful scheduled deletion: inspect native last-exit status and JSON logs, then repeat a report with the installed helper/config. Recheck executable paths after Python/Docker upgrades. Rollback: `launchctl bootout gui/$(id -u)/ai.marketsignal.image-retention`, remove only this newly installed plist, and restore the backed-up helper/config/plist if replacing an earlier installation. Bootstrap the restored plist only if it was previously loaded. Do not remove application data or unrelated agents.

## VM installation prerequisite and deployment hook

There is **no new VM timer**. Growth from deployments is handled by the serialized deploy workflow before its first pull and before Phase 4 changes image metadata. A prospective backend/frontend target is an explicit additional pin. `MSAI_IMAGE_MIN_FREE_BYTES` overrides the default 5368709120-byte deployment floor. Missing installed helper/config, wrong daemon, unsafe inventory/authority, overlap or insufficient headroom emits `FAIL_IMAGE_RETENTION` and refuses before changing image metadata. Existing app failure/rollback handling remains in place.

Generate the concrete VM config for review:

```bash
python3 scripts/install-image-retention.py --target vm
```

Use the emitted `config` object, not the surrounding preview JSON, for `/etc/msai/image-retention.json`. Defaults pin the observed VM `default` daemon and exact ACR backend/frontend repos, set zero newest-version reserve and leave explicit `protected_refs` empty; current/last-good authorities and all container references supply release protection. Change the target explicitly for a rehearsal VM, preserving any installed/operator pins. The installer does not install on Linux. After operational approval, stage only the reviewed stdlib helper at `/usr/local/lib/msai/docker-image-retention.py` (root-owned, mode 0644), config at `/etc/msai/image-retention.json` (root-owned, mode 0600), and use `/usr/bin/python3` >=3.10. Back up any prior installed files. Do not install a VM scheduler.

Before enabling the guard, take a current quiet-deployment/fleet check through the release-safety flow. Inspect current and last-good image assignments as data; do not source/eval them for maintenance. Preserve `/run/msai-images.env` and `/run/msai-images.last-good.env` into `/opt/msai/msai-images.env` and `/opt/msai/msai-images.last-good.env` atomically under umask 077, without replacing a differing persistent authority until reconciled. Both copies, when present, supply protected refs. Missing current/last-good authority refuses; malformed copies refuse even if another copy is valid. For an explicitly verified fresh first deployment only, initialize absent current and last-good authorities with `MSAI_FIRST_DEPLOY=1`; never use this sentinel to bypass lost metadata on an existing VM.

Run a report with the installed helper/config. A **manual VM apply** additionally requires a fresh quiet-deployment check and `--quiet-deployment` (an operator attestation, not a global lock), plus approved full IDs or recurring policy. The normal deploy hook passes that flag within the existing serialized/quiet workflow. The helper does not call trading APIs or stop deployments.

Phase 4 recovers current from its persistent mirror after reboot, snapshots current as both volatile/persistent last-good, then records the prospective current pair. Rollback refreshes the persistent current mirror as well as `/run`. First-deploy sentinel behavior remains explicit. The workflow conditionally stages the helper only if the selected application payload contains it, so dispatching a pre-fix SHA can stage successfully. It does not silently overwrite the stable operator-reviewed helper/config, and an old application SHA's old deploy script does not gain the new guard/persistence code. Confirm/restore the persistent metadata after any such historical rollback before maintenance or reboot.

After an approved cleanup, compare full running/stopped container/image/volume inventories, host available bytes, Docker disk usage and protected pins with the preflight; repeat a report. Disabling/restoring maintenance does not recover deleted layers. Registry images may be re-pulled, but deletion is not universally reversible. No permanent prevention claim is justified until installation and actual recurring/deploy behavior are observed on both engines.

## Verification scope

`python3 -m unittest discover -s tests/infra -p 'test_image_retention.py' -v` exercises the actual CLI against an external Docker fixture, creation/reserve/container/alias/pin protections, daemon/inventory refusal, native-conflict reporting, local activity/nonoverlap and disk pressure. Deployment shell fixtures prove pre-metadata refusal, reboot current recovery and persistent rollback. Installer previews verify stable paths and pin merging. Disposable native Docker E2E, real engine reports and approved operational installation are separate evidence; these source tests do not certify them.
