#!/usr/bin/env python3
"""Read-only release checks. Python 3.9+; requires gh and curl on PATH."""
import argparse
from datetime import datetime
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

REQUIRED = {
    "ci.yml": ("backend", "frontend", "image-data-path"),
    "auth-gate.yml": ("Gate 1 — MSAL scope lint", "Gates 2+3 — JWT contract + identity-contract lint"),
}
PENDING = {"queued", "in_progress", "waiting", "pending", "requested"}


class Refusal(RuntimeError):
    """A release precondition could not be proved."""


def gh_json(endpoint, timeout=30):
    try:
        result = subprocess.run(
            ["gh", "api", "--method", "GET", "-H", "X-GitHub-Api-Version: 2022-11-28", endpoint],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("GitHub read unavailable or timed out; check gh authentication/connectivity") from exc
    if result.returncode:
        raise Refusal(f"GitHub read failed (exit {result.returncode}); check Actions read permission and retained evidence")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise Refusal("GitHub returned malformed JSON") from exc


def repository_name(repo):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise Refusal("repository must be OWNER/REPO")
    return repo


def positive_int(value):
    return type(value) is int and value > 0


def resolve_target(repo, event, manual_sha, upstream_sha, default_sha):
    repository_name(repo)
    if event == "workflow_dispatch":
        if manual_sha:
            if not re.fullmatch(r"[0-9a-f]{7}", manual_sha):
                raise Refusal("manual git_sha must be exactly seven lowercase hex characters")
            response = gh_json(f"/repos/{repo}/commits/{manual_sha}")
            sha = response.get("sha") if isinstance(response, dict) else None
            if not isinstance(sha, str) or not sha.startswith(manual_sha):
                raise Refusal("manual git_sha did not resolve to the requested commit")
        else:
            sha = default_sha
    elif event == "workflow_run" and not manual_sha:
        sha = upstream_sha
    else:
        raise Refusal("unsupported release event or conflicting manual target")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Refusal("target must resolve to one full 40-character SHA")
    return sha


def all_pages(api, endpoint, key):
    """Bound the provider's filtered 1000-result cap and reject partial evidence."""
    result, total = [], None
    separator = "&" if "?" in endpoint else "?"
    for page in range(1, 11):
        body = api(f"{endpoint}{separator}per_page=100&page={page}")
        if not isinstance(body, dict) or type(body.get("total_count")) is not int:
            raise Refusal("GitHub pagination lacks an integer total_count")
        count, rows = body["total_count"], body.get(key)
        if count < 0 or count >= 1000 or not isinstance(rows, list) or len(rows) > 100:
            raise Refusal("GitHub pagination is malformed or exceeds the 1000-result bound")
        if total is not None and count != total:
            raise Refusal("GitHub pagination changed during inspection; retry")
        total = count
        result.extend(rows)
        if len(result) == total:
            ids = [r.get("id") if isinstance(r, dict) else None for r in result]
            if any(not positive_int(i) for i in ids) or len(set(ids)) != len(ids):
                raise Refusal("GitHub evidence has missing or duplicate identities")
            return result
        if len(result) > total or len(rows) < 100:
            raise Refusal("GitHub pagination is incomplete or inconsistent")
    raise Refusal("GitHub pagination did not complete within ten pages")


def valid_run(run, repo, workflow, workflow_id, sha):
    expected = {"path": f".github/workflows/{workflow}", "workflow_id": workflow_id,
                "head_sha": sha, "head_branch": "main", "event": "push"}
    if not isinstance(run, dict) or any(run.get(k) != v for k, v in expected.items()):
        raise Refusal(f"{workflow}: wrong revision, workflow, event or branch identity")
    if any(not isinstance(run.get(k), dict) or run[k].get("full_name") != repo
           for k in ("repository", "head_repository")):
        raise Refusal(f"{workflow}: wrong or missing repository identity")
    if any(not positive_int(run.get(k)) for k in ("id", "run_attempt", "workflow_id")):
        raise Refusal(f"{workflow}: malformed run/attempt identity")
    try:
        created = datetime.strptime(run["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, ValueError, TypeError) as exc:
        raise Refusal(f"{workflow}: malformed run creation time") from exc
    return created, run["id"]


def check_release(repo, sha, wait_seconds=1800):
    repository_name(repo)
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not 0 <= wait_seconds <= 1800:
        raise Refusal("check needs a full SHA and wait-seconds between 0 and 1800")
    deadline = time.monotonic() + wait_seconds

    def api(endpoint):
        # A zero wait still permits one read-only inspection.
        remaining = deadline - time.monotonic() if wait_seconds else 30
        if remaining <= 0:
            raise Refusal(f"CI_WAIT_TIMEOUT: {sha}; required checks did not finish within {wait_seconds}s")
        return gh_json(endpoint, timeout=min(30, remaining))

    while True:
        pending, verified = [], []
        for workflow, names in REQUIRED.items():
            meta = api(f"/repos/{repo}/actions/workflows/{workflow}")
            if not isinstance(meta, dict) or not positive_int(meta.get("id")) or meta.get("path") != f".github/workflows/{workflow}":
                raise Refusal(f"{workflow}: workflow identity unavailable")
            endpoint = f"/repos/{repo}/actions/workflows/{workflow}/runs?head_sha={sha}&event=push&branch=main"

            def latest():
                runs = all_pages(api, endpoint, "workflow_runs")
                if not runs:
                    raise Refusal(f"{workflow}: missing main-push run for {sha}; historical evidence has no bypass")
                return max(runs, key=lambda r: valid_run(r, repo, workflow, meta["id"], sha))

            run = latest()
            identity = run["id"], run["run_attempt"]
            if run.get("status") in PENDING and run.get("conclusion") is None:
                pending.append(f"{workflow} run={identity[0]} attempt={identity[1]} status={run['status']}")
                continue
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                raise Refusal(f"{workflow} run={identity[0]} attempt={identity[1]} is not completed/success")
            jobs = all_pages(api, f"/repos/{repo}/actions/runs/{identity[0]}/attempts/{identity[1]}/jobs", "jobs")
            for name in names:
                matches = [j for j in jobs if j.get("name") == name]
                if len(matches) != 1:
                    raise Refusal(f"{workflow}: required job {name!r} missing/duplicate; rerun ALL jobs")
                job = matches[0]
                if (job.get("status"), job.get("conclusion"), job.get("head_sha"), job.get("run_id"), job.get("run_attempt")) != ("completed", "success", sha, *identity):
                    raise Refusal(f"{workflow}: required job {name!r} is not successful for this SHA/attempt; rerun ALL jobs")
                if not positive_int(job.get("run_id")) or not positive_int(job.get("run_attempt")):
                    raise Refusal(f"{workflow}: malformed job run/attempt")
            fresh = api(f"/repos/{repo}/actions/runs/{identity[0]}")
            valid_run(fresh, repo, workflow, meta["id"], sha)
            newest = latest()
            if any((r.get("id"), r.get("run_attempt"), r.get("status"), r.get("conclusion")) != (*identity, "completed", "success") for r in (fresh, newest)):
                raise Refusal(f"{workflow}: latest run/attempt changed during verification; retry")
            verified.append(f"{workflow} run={identity[0]} attempt={identity[1]}")
        if not pending:
            print(f"RELEASE_CHECKS_OK: {sha}; " + "; ".join(verified))
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refusal(f"CI_WAIT_TIMEOUT: {sha}; " + "; ".join(pending))
        print(f"CI_WAIT: {sha}; " + "; ".join(pending), flush=True)
        time.sleep(min(15, remaining))


def check_readiness(host=None, bootstrap=False, base_url=None):
    if bool(host) == bool(base_url):
        raise Refusal("supply exactly one of host or base-url")
    base = base_url if base_url else f"https://{host}"
    parsed = urlsplit(base)
    if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"))):
        raise Refusal("readiness requires HTTPS; explicit HTTP base-url is allowed only on loopback")
    key = os.environ.get("MSAI_API_KEY")
    if not key or "\n" in key or "\r" in key:
        raise Refusal("MSAI_API_KEY is missing or invalid")
    try:
        # Header on stdin keeps the API key out of argv and diagnostics.
        result = subprocess.run(["curl", "--silent", "--fail", "--max-time", "10", "--header", "@-",
                                 "--write-out", "\n%{http_code}",
                                 base.rstrip("/") + "/api/v1/live/release-readiness"],
                                input=f"X-API-Key: {key}\n", text=True, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("READINESS_UNAVAILABLE: curl could not finish") from exc
    if result.returncode:
        if bootstrap and result.returncode in (6, 7):
            print(f"FRESH_BOOTSTRAP: explicit DNS/connect exception (curl {result.returncode})")
            return
        raise Refusal(f"READINESS_UNAVAILABLE: curl exit {result.returncode}; cannot prove quiet fleet")
    payload, separator, http_status = result.stdout.rpartition("\n")
    if not separator or http_status != "200":
        raise Refusal("READINESS_UNAVAILABLE: expected HTTP 200; redirects and other responses refuse")
    try:
        body = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise Refusal("READINESS_CONTRACT: empty/malformed response; old APIs require supervised installation") from exc
    if (not isinstance(body, dict) or type(body.get("contract_version")) is not int or body["contract_version"] != 1
            or body.get("scope") != "fleet" or body.get("complete") is not True or type(body.get("ready")) is not bool):
        raise Refusal("READINESS_CONTRACT: need v1 complete fleet contract; old APIs require supervised installation")
    fields = ("blocking_deployments", "blocking_processes", "restart_blockers")
    if any(type(body.get(k)) is not int or body[k] < 0 for k in fields):
        raise Refusal("READINESS_CONTRACT: blocker counts must be nonnegative integers")
    if not body["ready"] or any(body[k] for k in fields):
        raise Refusal("FLEET_NOT_READY: " + ", ".join(f"{k}={body[k]}" for k in fields))
    print("READINESS_OK: complete persisted fleet snapshot; maintain no-start/no-resume window")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser("resolve", help="resolve one exact application revision; emit full_sha and short_sha")
    resolve.add_argument("--repository", required=True)
    resolve.add_argument("--event", required=True, choices=("workflow_dispatch", "workflow_run"))
    resolve.add_argument("--manual-sha", default="")
    resolve.add_argument("--upstream-sha", default="")
    resolve.add_argument("--default-sha", default="")
    check = commands.add_parser("check", help="require CI and auth main-push success on this full SHA")
    check.add_argument("--repository", required=True)
    check.add_argument("--sha", required=True)
    check.add_argument("--wait-seconds", type=int, default=1800, help="pending-check wait bound, 0..1800 (default 1800)")
    readiness = commands.add_parser("readiness", help="read authenticated complete fleet readiness; MSAI_API_KEY required")
    target = readiness.add_mutually_exclusive_group(required=True)
    target.add_argument("--host", help="HTTPS hostname, optionally with port")
    target.add_argument("--base-url", help="explicit origin; HTTP allowed only for loopback verification")
    readiness.add_argument("--bootstrap", choices=("true", "false"), default="false", help="fresh targets only: curl 6/7 exception")
    args = parser.parse_args()
    try:
        if args.command == "resolve":
            sha = resolve_target(args.repository, args.event, args.manual_sha, args.upstream_sha, args.default_sha)
            value = f"full_sha={sha}\nshort_sha={sha[:7]}\n"
            print(value, end="")
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
                    stream.write(value)
        elif args.command == "check":
            check_release(args.repository, args.sha, args.wait_seconds)
        else:
            check_readiness(args.host, args.bootstrap == "true", args.base_url)
    except (Refusal, ValueError) as exc:
        print(f"RELEASE_REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
