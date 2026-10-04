#!/usr/bin/env python3
"""Own temporary SSH access narrowly. Python 3.9+; gh and az must be authenticated."""
import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys

from release_safety import Refusal, gh_json, positive_int, repository_name

PHASES = {"deploy": (200, "", "deploy.yml"), "preflight": (201, "preflight-", "deploy.yml"),
          "smoke": (202, "smoke-", "smoke.yml")}


def azure_failure_detail(stderr: str) -> str:
    """Expose only bounded symbolic codes or fixed categories, never message text."""
    for line in stderr.splitlines():
        code = re.match(r"^ERROR: \(([A-Za-z][A-Za-z0-9]{0,63})\)(?:[ \t]|$)", line)
        code = code or re.fullmatch(r"Code: ([A-Za-z][A-Za-z0-9]{0,63})[ \t]*", line)
        if code:
            return f"code={code[1]}"
    lowered = stderr.lower()
    for category, markers in (
        ("CLI_ARGUMENT_ERROR", ("unrecognized arguments:", "the following arguments are required:", "invalid choice:")),
        ("AUTHENTICATION_REQUIRED", ("please run 'az login'",)),
        ("SUBSCRIPTION_SELECTION", ("doesn't exist in cloud", "no subscriptions found")),
        ("CONNECTION_OR_TLS", ("connectionerror", "sslerror", "certificate verify failed")),
        ("CLI_RUNTIME_ERROR", ("traceback (most recent call last):",)),
    ):
        if any(marker in lowered for marker in markers):
            return f"category={category}"
    return "category=UNCLASSIFIED (details withheld)"


def az_json(args):
    try:
        response = subprocess.run(["az", "network", "nsg", "rule", *args, "--output", "json"],
                                  text=True, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal(f"Azure {args[0]} unavailable/timed out; rule state is unproved") from exc
    if response.returncode:
        raise Refusal(f"Azure {args[0]} failed (exit {response.returncode}); check subscription, RBAC and connectivity; "
                      f"{azure_failure_detail(response.stderr)}")
    try:
        return json.loads(response.stdout) if response.stdout.strip() else None
    except ValueError as exc:
        raise Refusal(f"Azure {args[0]} returned malformed JSON") from exc


def scope(rg, nsg, subscription):
    if not all(isinstance(v, str) and v and not v.startswith("-") for v in (rg, nsg, subscription)):
        raise Refusal("resource-group, nsg-name and subscription are required")
    return ["--resource-group", rg, "--nsg-name", nsg, "--subscription", subscription]


def list_rules(target):
    rules = az_json(["list", *target])
    if not isinstance(rules, list) or any(not isinstance(r, dict) or not isinstance(r.get("name"), str) for r in rules):
        raise Refusal("Azure list is not a complete rule list")
    if len({r["name"] for r in rules}) != len(rules):
        raise Refusal("Azure list returned duplicate names")
    return rules


def identity(repo, phase, run_id, attempt):
    repository_name(repo)
    if phase not in PHASES or not positive_int(run_id) or not positive_int(attempt):
        raise Refusal("invalid rule phase/run/attempt")
    priority, prefix, workflow = PHASES[phase]
    name = f"gha-transient-{prefix}{run_id}-{attempt}"
    description = f"msai-ssh:v1;repo={repo};workflow={workflow};run={run_id};attempt={attempt};phase={phase}"
    if len(description) > 140:
        raise Refusal("ownership description exceeds Azure's 140-character limit")
    return name, description, priority


def single(rule, singular, plural):
    one, many = rule.get(singular), rule.get(plural)
    if one not in (None, "") and many not in (None, []):
        return None
    if one not in (None, ""):
        return one
    return many[0] if isinstance(many, list) and len(many) == 1 else None


def owner(rule, repo):
    match = re.fullmatch(r"gha-transient-(?:(preflight|smoke)-)?([1-9][0-9]*)-([1-9][0-9]*)", rule.get("name", ""))
    if not match:
        return None
    phase, run_id, attempt = match[1] or "deploy", int(match[2]), int(match[3])
    _, description, priority = identity(repo, phase, run_id, attempt)
    legacy_source = {"deploy": "GHA", "preflight": "GHA deploy.yml preflight", "smoke": "GHA smoke.yml"}[phase]
    legacy = f"Transient SSH from {legacy_source}. run_id={run_id} attempt={attempt}. See docs/decisions/deploy-ssh-jit.md"
    if (rule.get("description") not in (description, legacy) or type(rule.get("priority")) is not int
            or rule["priority"] != priority or rule.get("direction") != "Inbound"
            or rule.get("access") != "Allow" or rule.get("protocol") != "Tcp"
            or rule.get("sourceApplicationSecurityGroups") or rule.get("destinationApplicationSecurityGroups")):
        return None
    for singular, plural, wanted in (("sourcePortRange", "sourcePortRanges", "*"),
                                     ("destinationPortRange", "destinationPortRanges", "22"),
                                     ("destinationAddressPrefix", "destinationAddressPrefixes", "*")):
        if single(rule, singular, plural) != wanted:
            return None
    try:
        source = single(rule, "sourceAddressPrefix", "sourceAddressPrefixes")
        network = ipaddress.ip_network(source, strict=True)
        if network.version != 4 or network.prefixlen != 32 or source != str(network):
            return None
    except (ValueError, TypeError):
        return None
    return phase, run_id, attempt


def attempt_status(repo, owned):
    phase, run_id, attempt = owned
    result = gh_json(f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}")
    events = {"schedule", "workflow_dispatch"} if phase == "smoke" else {"workflow_run", "workflow_dispatch"}
    if not isinstance(result, dict) or (result.get("id"), result.get("run_attempt")) != (run_id, attempt):
        raise Refusal("GitHub owner run/attempt identity is unproved")
    if not positive_int(result.get("id")) or not positive_int(result.get("run_attempt")):
        raise Refusal("GitHub owner run/attempt identity is malformed")
    if (result.get("path") != f".github/workflows/{PHASES[phase][2]}"
            or result.get("event") not in events or result.get("head_branch") != "main"
            or any(not isinstance(result.get(k), dict) or result[k].get("full_name") != repo
                   for k in ("repository", "head_repository"))):
        raise Refusal("GitHub owner repository/workflow/event/main identity is unproved")
    status = result.get("status")
    if status not in ("completed", "queued", "in_progress", "waiting", "pending", "requested"):
        raise Refusal("GitHub owner attempt status is unknown")
    return status


def delete_proved(target, repo, rule, owned, own_cleanup=False):
    current = next((r for r in list_rules(target) if r["name"] == rule["name"]), None)
    if current is None:
        print(f"NSG_ABSENT: {rule['name']}")
        return
    if current != rule or owner(current, repo) != owned:
        raise Refusal(f"rule {rule['name']} changed before deletion; preserved")
    status = attempt_status(repo, owned)
    if status != "completed" and not own_cleanup:
        raise Refusal(f"rule {rule['name']} belongs to active attempt; preserved")
    az_json(["delete", *target, "--name", rule["name"]])
    if any(r["name"] == rule["name"] for r in list_rules(target)):
        raise Refusal(f"delete did not prove absence for {rule['name']}")
    print(f"NSG_ABSENT: {rule['name']}")


def create(rg, nsg, repo, phase, run_id, attempt, runner_ip, subscription):
    name, description, priority = identity(repo, phase, run_id, attempt)
    try:
        address = ipaddress.IPv4Address(runner_ip)
    except ipaddress.AddressValueError as exc:
        raise Refusal("runner-ip must be a valid IPv4 address") from exc
    target = scope(rg, nsg, subscription)
    # Refuse before any deletion/create if the proposed owner would be unprovable
    # to cleanup (for example a manual workflow dispatched on a feature branch).
    attempt_status(repo, (phase, run_id, attempt))
    conflicts = [r for r in list_rules(target) if r["name"] == name or (r.get("priority") == priority and r.get("direction") == "Inbound")]
    for rule in conflicts:
        owned = owner(rule, repo)
        if owned is None:
            raise Refusal(f"NSG_CONFLICT: {rule['name']} ownership/shape unproved; preserved")
        if attempt_status(repo, owned) != "completed":
            raise Refusal(f"NSG_CONFLICT: {rule['name']} owning attempt is active; preserved")
        delete_proved(target, repo, rule, owned)
    # A fresh list catches a conflict arriving during recovery before create.
    if any(r["name"] == name or (r.get("priority") == priority and r.get("direction") == "Inbound") for r in list_rules(target)):
        raise Refusal("NSG_CONFLICT: priority/name became occupied before creation")
    az_json(["create", *target, "--name", name, "--priority", str(priority), "--direction", "Inbound",
             "--access", "Allow", "--protocol", "Tcp", "--source-address-prefixes", f"{address}/32",
             "--source-port-ranges", "*", "--destination-address-prefixes", "*", "--destination-port-ranges", "22",
             "--description", description])
    print(f"NSG_CREATED: {name} priority={priority}")


def cleanup(rg, nsg, repo, phase, run_id, attempt, producer_result, subscription, producer_rule_name):
    identity(repo, phase, run_id, attempt)
    if producer_result not in ("success", "failure", "cancelled", "skipped"):
        raise Refusal("cleanup requires a finished producer dependency")
    if (os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID"), os.environ.get("GITHUB_RUN_ATTEMPT")) != (repo, str(run_id), str(attempt)):
        raise Refusal("cleanup caller does not match current GitHub repository/run/attempt")
    # A cleanup-only rerun increments the caller attempt, but the successful
    # producer's recorded output still names the earlier rule that needs closing.
    prefix = PHASES[phase][1]
    match = re.fullmatch(rf"gha-transient-{prefix}{run_id}-([1-9][0-9]*)", producer_rule_name)
    if not match or int(match[1]) > attempt:
        raise Refusal("producer rule must name this phase/run and a current or earlier attempt")
    producer_attempt = int(match[1])
    name = producer_rule_name
    target = scope(rg, nsg, subscription)
    rule = next((r for r in list_rules(target) if r["name"] == name), None)
    if rule is None:
        print(f"NSG_ABSENT: {name}")
        return
    owned = owner(rule, repo)
    if owned != (phase, run_id, producer_attempt):
        raise Refusal(f"cleanup ownership/shape mismatch for {name}; preserved")
    own_cleanup = producer_attempt == attempt
    if attempt_status(repo, owned) != "completed" and not own_cleanup:
        raise Refusal(f"rule {name} belongs to active earlier attempt; preserved")
    delete_proved(target, repo, rule, owned, own_cleanup=own_cleanup)


def reap(rg, nsg, repo, subscription, dry_run=False):
    repository_name(repo)
    target = scope(rg, nsg, subscription)
    for rule in list_rules(target):
        owned = owner(rule, repo)
        if owned is None:
            print(f"NSG_PRESERVED: {rule['name']} ownership/shape unproved")
            continue
        if attempt_status(repo, owned) != "completed":
            print(f"NSG_PRESERVED: {rule['name']} owning attempt active")
        elif dry_run:
            print(f"NSG_WOULD_DELETE: {rule['name']} completed owner attempt")
        else:
            delete_proved(target, repo, rule, owned)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "cleanup", "reap"):
        sub = commands.add_parser(command, help={"create": "recover completed-owned priority conflict, then open SSH",
                                                "cleanup": "close own finished producer's rule; requires GITHUB_* identity",
                                                "reap": "close only rules with proved completed owning attempts"}[command])
        for arg in ("resource-group", "nsg-name", "repository", "subscription"):
            sub.add_argument(f"--{arg}", required=True)
        if command == "reap":
            sub.add_argument("--dry-run", action="store_true", help="read-only candidate/preservation decisions; no Azure mutations")
        else:
            sub.add_argument("--phase", required=True, choices=tuple(PHASES))
            sub.add_argument("--run-id", required=True, type=int)
            sub.add_argument("--attempt", required=True, type=int)
            if command == "create":
                sub.add_argument("--runner-ip", required=True)
            else:
                sub.add_argument("--producer-result", required=True, choices=("success", "failure", "cancelled", "skipped"))
                sub.add_argument("--producer-rule-name", required=True, help="exact rule_name output retained from the producer job, including on cleanup-only reruns")
    args = parser.parse_args()
    try:
        shared = args.resource_group, args.nsg_name, args.repository
        if args.command == "reap":
            reap(*shared, args.subscription, args.dry_run)
        elif args.command == "create":
            create(*shared, args.phase, args.run_id, args.attempt, args.runner_ip, args.subscription)
        else:
            cleanup(*shared, args.phase, args.run_id, args.attempt, args.producer_result, args.subscription, args.producer_rule_name)
    except Refusal as exc:
        print(f"NSG_REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
