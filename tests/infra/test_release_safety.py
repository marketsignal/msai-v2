"""Release policy regression tests; provider boundaries are controlled offline."""
import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SHA = "a" * 40
REPO = "marketsignal/msai-v2"
WORKFLOWS = {
    "ci.yml": ["backend", "frontend", "image-data-path"],
    "auth-gate.yml": ["Gate 1 — MSAL scope lint", "Gates 2+3 — JWT contract + identity-contract lint"],
}


def run_record(workflow="ci.yml", run_id=10, attempt=1, **overrides):
    value = dict(id=run_id, run_attempt=attempt, workflow_id=1 if workflow == "ci.yml" else 2,
                 path=f".github/workflows/{workflow}", head_sha=SHA, head_branch="main",
                 event="push", status="completed", conclusion="success",
                 created_at="2026-10-03T00:00:00Z", repository={"full_name": REPO},
                 head_repository={"full_name": REPO})
    value.update(overrides)
    return value


class GitHubFixture:
    def __init__(self):
        self.runs = {w: [run_record(w, i * 10)] for i, w in enumerate(WORKFLOWS, 1)}
        self.jobs = {w: [dict(id=i, name=n, status="completed", conclusion="success",
                             head_sha=SHA, run_attempt=1, run_id=(10 if w == "ci.yml" else 20))
                         for i, n in enumerate(names, 1)] for w, names in WORKFLOWS.items()}
        self.calls = []
        self.override = None

    def __call__(self, endpoint, **kwargs):
        self.calls.append(endpoint)
        if self.override:
            result = self.override(endpoint)
            if result is not None:
                return copy.deepcopy(result)
        if "/commits/" in endpoint:
            return {"sha": SHA}
        for w in WORKFLOWS:
            if f"/workflows/{w}/runs?" in endpoint:
                self.assert_query(endpoint)
                return {"total_count": len(self.runs[w]), "workflow_runs": copy.deepcopy(self.runs[w])}
            if endpoint.endswith(f"/workflows/{w}"):
                return {"id": 1 if w == "ci.yml" else 2, "path": f".github/workflows/{w}"}
            for run in self.runs[w]:
                if f"/runs/{run['id']}/attempts/" in endpoint:
                    return {"total_count": len(self.jobs[w]), "jobs": copy.deepcopy(self.jobs[w])}
                if endpoint.endswith(f"/runs/{run['id']}"):
                    return copy.deepcopy(run)
        raise AssertionError(f"unexpected provider request: {endpoint}")

    @staticmethod
    def assert_query(endpoint):
        for part in (f"head_sha={SHA}", "event=push", "branch=main", "per_page=100", "page="):
            assert part in endpoint, endpoint
        assert "status=success" not in endpoint


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "scripts/release_safety.py").exists(), "release helper is missing")
        self.module = importlib.import_module("release_safety")
        self.gh = GitHubFixture()
        self.mock = patch.object(self.module, "gh_json", self.gh)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def check(self, **kwargs):
        return self.module.check_release(REPO, SHA, wait_seconds=0, **kwargs)

    def test_exact_revision_success_and_manual_automatic_resolution(self):
        self.check()
        self.assertEqual(self.module.resolve_target(REPO, "workflow_dispatch", SHA[:7], "", "b" * 40), SHA)
        self.assertEqual(self.module.resolve_target(REPO, "workflow_run", "", SHA, "b" * 40), SHA)
        self.assertEqual(self.module.resolve_target(REPO, "workflow_dispatch", "", "", SHA), SHA)
        for manual in ("main", "abc", "aaaaaaa;echo bad"):
            with self.assertRaises(self.module.Refusal):
                self.module.resolve_target(REPO, "workflow_dispatch", manual, "", SHA)
        self.assertGreaterEqual(sum("/workflows/ci.yml/runs?" in c for c in self.gh.calls), 2)

    def test_green_wrong_revision_or_workflow_identity_never_qualifies(self):
        for field, bad in (("head_sha", "b" * 40), ("head_branch", "branch"), ("event", "pull_request"),
                           ("path", ".github/workflows/other.yml"), ("workflow_id", 999),
                           ("repository", {"full_name": "other/repo"}),
                           ("head_repository", {"full_name": "other/repo"}), ("run_attempt", True)):
            with self.subTest(field=field):
                self.gh.runs["ci.yml"] = [run_record(**{field: bad})]
                with self.assertRaises(self.module.Refusal):
                    self.check()

    def test_missing_failed_cancelled_skipped_or_pending_refuses(self):
        for status, conclusion in (("completed", "failure"), ("completed", "cancelled"),
                                   ("completed", "skipped"), ("in_progress", None), ("queued", None)):
            self.gh.runs["ci.yml"] = [run_record(status=status, conclusion=conclusion)]
            with self.subTest(status=status, conclusion=conclusion), self.assertRaises(self.module.Refusal):
                self.check()
        self.gh.runs["ci.yml"] = []
        with self.assertRaisesRegex(self.module.Refusal, "missing|Missing|No .*run"):
            self.check()

    def test_latest_run_wins_over_old_success(self):
        for latest in (run_record(run_id=11, conclusion="failure"), run_record(run_id=11, status="queued", conclusion=None)):
            self.gh.runs["ci.yml"] = [run_record(), latest]
            with self.assertRaises(self.module.Refusal):
                self.check()

    def test_required_jobs_must_be_unique_successful_in_current_attempt(self):
        original = copy.deepcopy(self.gh.jobs["ci.yml"])
        cases = [original[:-1], original + [original[0]]]
        for field, value in (("status", "queued"), ("conclusion", "skipped"), ("head_sha", "b" * 40),
                             ("run_attempt", 2), ("run_id", 99)):
            changed = copy.deepcopy(original)
            changed[0][field] = value
            cases.append(changed)
        for jobs in cases:
            self.gh.jobs["ci.yml"] = jobs
            with self.subTest(jobs=jobs), self.assertRaises(self.module.Refusal):
                self.check()

    def test_rerun_or_newer_run_arriving_during_inspection_refuses(self):
        def rerun(endpoint):
            if endpoint.endswith("/runs/10"):
                return run_record(attempt=2)
        self.gh.override = rerun
        with self.assertRaises(self.module.Refusal):
            self.check()
        count = 0
        def newer(endpoint):
            nonlocal count
            if "/workflows/ci.yml/runs?" in endpoint:
                count += 1
                if count > 1:
                    return {"total_count": 2, "workflow_runs": [run_record(), run_record(run_id=11)]}
        self.gh.override = newer
        with self.assertRaises(self.module.Refusal):
            self.check()

    def test_pagination_incomplete_malformed_or_api_failure_refuses(self):
        for response in ({}, {"total_count": True, "workflow_runs": []},
                         {"total_count": 1001, "workflow_runs": []},
                         {"total_count": 2, "workflow_runs": [run_record()]},
                         {"total_count": 1, "workflow_runs": [run_record(), run_record()]}):
            self.gh.override = lambda endpoint, response=response: response if "/runs?" in endpoint else None
            with self.subTest(response=response), self.assertRaises(self.module.Refusal):
                self.check()
        with patch.object(self.module, "gh_json", side_effect=self.module.Refusal("GitHub unavailable")):
            with self.assertRaises(self.module.Refusal):
                self.check()

    def test_complete_pagination_and_bounded_pending_wait(self):
        records = [run_record(run_id=i) for i in range(1, 102)]
        self.gh.runs["ci.yml"] = [records[-1]]
        for job in self.gh.jobs["ci.yml"]:
            job["run_id"] = 101
        self.gh.override = lambda endpoint: {"total_count": 101, "workflow_runs": records[100:] if "page=2" in endpoint else records[:100]} if "/workflows/ci.yml/runs?" in endpoint else None
        self.check()
        self.assertTrue(any("page=2" in c for c in self.gh.calls))
        self.gh.override = None
        self.gh.runs["ci.yml"] = [run_record(status="queued", conclusion=None)]
        with patch.object(self.module.time, "monotonic", side_effect=[0, 0, 2, 2, 2]), patch.object(self.module.time, "sleep") as sleep:
            with self.assertRaises(self.module.Refusal):
                self.module.check_release(REPO, SHA, wait_seconds=1)
            self.assertLessEqual(sleep.call_count, 1)

    def test_strict_readiness_and_bootstrap(self):
        ready = {"contract_version": 1, "scope": "fleet", "complete": True, "ready": True,
                 "blocking_deployments": 0, "blocking_processes": 0, "restart_blockers": 0}
        invalid = ["", "{}", "[]", '{"deployments":[]}']
        for field, values in {"contract_version": [True, 2, "1"], "scope": ["account"],
                              "complete": [False, 1, "true"], "ready": [False, 1, "true"],
                              "blocking_deployments": [True, -1, 1, "0", 0.0],
                              "blocking_processes": [1], "restart_blockers": [1]}.items():
            for value in values:
                invalid.append(json.dumps({**ready, field: value}))
            invalid.append(json.dumps({k: v for k, v in ready.items() if k != field}))
        with patch.dict(os.environ, {"MSAI_API_KEY": "not-logged"}):
            for body in invalid:
                with patch.object(self.module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, body + "\n200", "")):
                    with self.subTest(body=body), self.assertRaises(self.module.Refusal):
                        self.module.check_readiness("example.test", bootstrap=True)
            for rc in (0, 6, 7, 22, 28, 35):
                for bootstrap in (False, True):
                    with patch.object(self.module.subprocess, "run", return_value=subprocess.CompletedProcess([], rc, json.dumps(ready) + "\n200", "secret")) as call:
                        if rc == 0 or (rc in (6, 7) and bootstrap):
                            self.module.check_readiness("example.test", bootstrap)
                        else:
                            with self.assertRaises(self.module.Refusal):
                                self.module.check_readiness("example.test", bootstrap)
                        argv = call.call_args.args[0]
                        self.assertIn("https://example.test/api/v1/live/release-readiness", argv)
                        self.assertIn("--max-time", argv)

    def test_local_readiness_transport_is_explicit_and_loopback_only(self):
        body = json.dumps(dict(contract_version=1, scope="fleet", complete=True, ready=True,
                               blocking_deployments=0, blocking_processes=0, restart_blockers=0))
        with patch.dict(os.environ, {"MSAI_API_KEY": "not-logged"}), patch.object(self.module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, body + "\n200", "")) as call:
            self.module.check_readiness(base_url="http://127.0.0.1:8801")
            self.assertIn("http://127.0.0.1:8801/api/v1/live/release-readiness", call.call_args.args[0])
            for url in ("http://example.com", "https://example.com/path", "https://user:pass@example.com", "file:///tmp/ready"):
                with self.subTest(url=url), self.assertRaises(self.module.Refusal):
                    self.module.check_readiness(base_url=url)

    def test_http_redirect_cannot_count_as_authenticated_readiness(self):
        # curl --fail accepts HTTP 3xx; an unsigned redirect body is not API proof.
        body = json.dumps(dict(contract_version=1, scope="fleet", complete=True, ready=True,
                               blocking_deployments=0, blocking_processes=0, restart_blockers=0))
        def curl(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, body + ("\n302" if "--write-out" in argv else ""), "")
        with patch.dict(os.environ, {"MSAI_API_KEY": "not-logged"}), patch.object(self.module.subprocess, "run", side_effect=curl):
            with self.assertRaises(self.module.Refusal):
                self.module.check_readiness("example.test")

    def test_bounded_pending_wait_can_become_successful(self):
        self.gh.runs["ci.yml"] = [run_record(status="queued", conclusion=None)]
        def complete(_seconds):
            self.gh.runs["ci.yml"] = [run_record()]
        with patch.object(self.module.time, "sleep", side_effect=complete) as sleep:
            self.module.check_release(REPO, SHA, wait_seconds=30)
        self.assertEqual(sleep.call_count, 1)


class WorkflowTests(unittest.TestCase):
    """Execute real YAML run blocks with fake external executables, never Azure."""
    def setUp(self):
        import yaml
        self.workflows = {name: yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())
                          for name in ("deploy", "smoke", "reap-orphan-nsg-rules", "ci")}
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.fixture = self.folder / "fixture.json"
        self.log = self.folder / "log.jsonl"
        self.output = self.folder / "output"
        provider = self.folder / "provider"
        provider.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
path = pathlib.Path(os.environ["FIXTURE"])
data = json.loads(path.read_text())
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["LOG"], "a") as stream:
    stream.write(json.dumps([name, args]) + "\\n")
if name == "gh":
    endpoint = args[-1]
    if endpoint not in data["gh"]:
        sys.exit(9)
    print(json.dumps(data["gh"][endpoint]))
elif name == "curl":
    if data.get("curl_exit", 0): sys.exit(data["curl_exit"])
    print(json.dumps(data["ready"]))
    if "--write-out" in args: print("200", end="")
elif name == "az":
    verb = args[3]
    if data.get("az_fail") == verb: sys.exit(8)
    if verb == "list": print(json.dumps(data["rules"]))
    elif verb == "delete":
        rule_name = args[args.index("--name") + 1]
        data["rules"] = [r for r in data["rules"] if r["name"] != rule_name]
        path.write_text(json.dumps(data))
    elif verb == "create":
        data["rules"].append({"name": args[args.index("--name") + 1]})
        path.write_text(json.dumps(data))
''')
        provider.chmod(0o755)
        for name in ("gh", "curl", "az"):
            (self.folder / name).symlink_to(provider)
        self.data = {"gh": {}, "rules": [], "ready": dict(contract_version=1, scope="fleet", complete=True,
                       ready=True, blocking_deployments=0, blocking_processes=0, restart_blockers=0)}
        fixture = GitHubFixture()
        for workflow, names in WORKFLOWS.items():
            run = fixture.runs[workflow][0]
            self.data["gh"][f"/repos/{REPO}/actions/workflows/{workflow}"] = {"id": run["workflow_id"], "path": run["path"]}
            self.data["gh"][f"/repos/{REPO}/actions/workflows/{workflow}/runs?head_sha={SHA}&event=push&branch=main&per_page=100&page=1"] = {"total_count": 1, "workflow_runs": [run]}
            self.data["gh"][f"/repos/{REPO}/actions/runs/{run['id']}"] = run
            self.data["gh"][f"/repos/{REPO}/actions/runs/{run['id']}/attempts/1/jobs?per_page=100&page=1"] = {"total_count": len(names), "jobs": fixture.jobs[workflow]}
        self.data["gh"][f"/repos/{REPO}/commits/{SHA[:7]}"] = {"sha": SHA}
        self.context = {"github.repository": REPO, "github.run_id": "10", "github.run_attempt": "1",
                        "github.event_name": "workflow_run", "github.event.workflow_run.head_sha": SHA,
                        "github.sha": "b" * 40, "github.workflow_sha": "c" * 40,
                        "inputs.git_sha": "", "inputs.bootstrap": "false",
                        "inputs.msai_hostname != '' && inputs.msai_hostname || vars.MSAI_HOSTNAME": "example.test",
                        "inputs.bootstrap || false": "false", "vars.AZURE_SUBSCRIPTION_ID": "sub-id",
                        "secrets.GITHUB_TOKEN": "test-gh-token", "secrets.MSAI_API_KEY": "not-logged",
                        "steps.target.outputs.full_sha": SHA, "needs.release-check.outputs.full_sha": SHA,
                        "needs.release-check.outputs.short_sha": SHA[:7], "steps.resolve.outputs.msai_hostname": "example.test"}
        self.env = {**os.environ, "PATH": f"{self.folder}:{os.environ['PATH']}", "FIXTURE": str(self.fixture),
                    "LOG": str(self.log), "GITHUB_OUTPUT": str(self.output), "GITHUB_REPOSITORY": REPO,
                    "GITHUB_RUN_ID": "10", "GITHUB_RUN_ATTEMPT": "1", "MSAI_API_KEY": "not-logged"}

    def expand(self, value):
        import re
        return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda m: self.context[m[1]], str(value))

    def execute(self, step):
        self.fixture.write_text(json.dumps(self.data))
        env = {**self.env, **{k: self.expand(v) for k, v in step.get("env", {}).items()}}
        script = self.expand(step["run"]).replace("control/scripts/", str(ROOT / "scripts") + "/")
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=10)
        self.data = json.loads(self.fixture.read_text())
        return result

    def test_release_job_executes_resolve_check_readiness_before_azure_dependencies(self):
        jobs = self.workflows["deploy"]["jobs"]
        self.assertIn("release-check", jobs, "release gate missing before Azure jobs")
        release = jobs["release-check"]
        for target in ("preflight", "deploy"):
            self.assertIn("release-check", jobs[target]["needs"])
            self.assertIn("needs.release-check.result == 'success'", jobs[target]["if"])
        steps = [s for s in release["steps"] if "run" in s]
        self.assertEqual(len(steps), 3)
        for step in steps:
            result = self.execute(step)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"full_sha={SHA}", self.output.read_text())
        self.context.update({"github.event_name": "workflow_dispatch", "inputs.git_sha": SHA[:7]})
        self.assertEqual(self.execute(steps[0]).returncode, 0)
        # A green B cannot satisfy A and the gate must never reach Azure.
        self.data["gh"][f"/repos/{REPO}/actions/runs/10"]["head_sha"] = "b" * 40
        self.assertNotEqual(self.execute(steps[1]).returncode, 0)
        self.assertNotIn('"az"', self.log.read_text())

    def test_workflow_job_conditions_refuse_failed_gate_and_failed_optional_preflight(self):
        import re
        jobs = self.workflows["deploy"]["jobs"]
        def allowed(job, values):
            expression = jobs[job]["if"].strip().removeprefix("${{").removesuffix("}}").strip()
            expression = expression.replace("always()", "True")
            expression = re.sub(r"\b(?:github|needs)\.[A-Za-z0-9_.-]+", lambda m: repr(values[m[0]]), expression)
            expression = expression.replace("&&", " and ").replace("||", " or ")
            return eval(" ".join(expression.split()), {"__builtins__": {}}, {})
        values = {"github.event_name": "workflow_dispatch", "github.event.inputs.run_smoke": "true",
                  "github.event.workflow_run.conclusion": "success", "needs.release-check.result": "failure",
                  "needs.preflight.result": "skipped"}
        for result in ("failure", "cancelled", "skipped"):
            values["needs.release-check.result"] = result
            self.assertFalse(allowed("preflight", values))
            self.assertFalse(allowed("deploy", values))
        values["needs.release-check.result"] = "success"
        self.assertTrue(allowed("preflight", values))
        self.assertTrue(allowed("deploy", values))
        values["needs.preflight.result"] = "failure"
        self.assertFalse(allowed("deploy", values))
        values.update({"github.event_name": "workflow_run", "needs.preflight.result": "skipped"})
        self.assertTrue(allowed("release-check", values))
        self.assertTrue(allowed("deploy", values))
        self.assertFalse(allowed("preflight", values))
        values["github.event.workflow_run.conclusion"] = "failure"
        self.assertFalse(allowed("release-check", values))
        self.assertFalse(allowed("deploy", values))

    def test_deploy_uses_current_controls_exact_payload_and_fresh_readiness_before_execute(self):
        jobs = self.workflows["deploy"]["jobs"]
        checkouts = [s["with"] for s in jobs["deploy"]["steps"] if "actions/checkout@" in s.get("uses", "")]
        self.assertIn({"ref": "${{ github.workflow_sha }}", "path": "control", "persist-credentials": False}, checkouts)
        self.assertIn({"ref": "${{ needs.release-check.outputs.full_sha }}", "path": "payload", "persist-credentials": False}, checkouts)
        steps = jobs["deploy"]["steps"]
        execute = next(i for i, step in enumerate(steps) if step.get("name") == "Execute deploy")
        gate = steps[execute - 1]
        self.assertIn("readiness", gate.get("run", ""))
        self.data["ready"]["restart_blockers"] = 1
        result = self.execute(gate)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FLEET_NOT_READY", result.stderr)

    def test_all_cleanup_callers_execute_same_strict_lifecycle_and_surface_delete_failure(self):
        from test_nsg_rule_lifecycle import rule
        for workflow, job, phase, producer in (("deploy", "cleanup", "deploy", "deploy"),
                                                ("deploy", "preflight-cleanup", "preflight", "preflight"),
                                                ("smoke", "cleanup", "smoke", "smoke")):
            with self.subTest(phase=phase):
                step = next(s for s in self.workflows[workflow]["jobs"][job]["steps"] if s.get("name") == "Delete transient SSH rule")
                self.assertIn("nsg_rule_lifecycle.py cleanup", step["run"])
                self.context.update({f"needs.{producer}.outputs.resource_group": "rg",
                                     f"needs.{producer}.outputs.nsg_name": "nsg", f"needs.{producer}.result": "success",
                                     f"needs.{producer}.outputs.rule_name": rule(phase)["name"]})
                self.data["rules"] = [rule(phase)]
                owner_workflow = "smoke.yml" if phase == "smoke" else "deploy.yml"
                self.data["gh"][f"/repos/{REPO}/actions/runs/10/attempts/1"] = dict(
                    id=10, run_attempt=1, status="in_progress", event="workflow_dispatch", head_branch="main",
                    path=f".github/workflows/{owner_workflow}", repository={"full_name": REPO}, head_repository={"full_name": REPO})
                self.data.pop("az_fail", None)
                result = self.execute(step)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.data["rules"], [])
                self.data["rules"] = [rule(phase)]
                self.data["az_fail"] = "delete"
                result = self.execute(step)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.data["rules"], [rule(phase)])
                self.assertNotIn("not-logged", result.stdout + result.stderr)

    def test_all_create_callers_and_reaper_execute_shared_policy(self):
        from test_nsg_rule_lifecycle import rule
        for workflow, job, phase, source, ip_source in (("deploy", "deploy", "deploy", "resolve", "runner-ip"),
                                                      ("deploy", "preflight", "preflight", "preflight-resolve", "preflight-runner-ip"),
                                                      ("smoke", "smoke", "smoke", "resolve", "runner-ip")):
            self.context.update({f"steps.{source}.outputs.resource_group": "rg", f"steps.{source}.outputs.nsg_name": "nsg",
                                 f"steps.{ip_source}.outputs.ip": "192.0.2.2"})
            step = next(s for s in self.workflows[workflow]["jobs"][job]["steps"] if s.get("name") == "Open transient SSH allow rule")
            self.data["rules"] = [rule(phase, run=9)]
            owner = dict(id=9, run_attempt=1, status="completed", event="workflow_dispatch", head_branch="main",
                         path=f".github/workflows/{'smoke' if phase == 'smoke' else 'deploy'}.yml",
                         repository={"full_name": REPO}, head_repository={"full_name": REPO})
            self.data["gh"][f"/repos/{REPO}/actions/runs/9/attempts/1"] = owner
            self.data["gh"][f"/repos/{REPO}/actions/runs/10/attempts/1"] = {**owner, "id": 10, "status": "in_progress"}
            result = self.execute(step)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(self.data["rules"]), 1)
            calls = [json.loads(line) for line in self.log.read_text().splitlines()]
            self.assertTrue(all("--subscription" in args for name, args in calls if name == "az"))
        self.data["rules"] = [rule("smoke", run=9)]
        self.context.update({"vars.RESOURCE_GROUP": "rg", "vars.NSG_NAME": "nsg"})
        step = next(s for s in self.workflows["reap-orphan-nsg-rules"]["jobs"]["reap"]["steps"] if "run" in s)
        result = self.execute(step)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.data["rules"], [])

    def test_cleanup_only_rerun_all_three_callers_removes_original_producer_rule(self):
        from test_nsg_rule_lifecycle import rule
        # The successful producer is NOT rerun: its output remains attempt 1.
        self.context["github.run_attempt"] = "2"
        self.env["GITHUB_RUN_ATTEMPT"] = "2"
        for workflow, job, phase, producer in (("deploy", "cleanup", "deploy", "deploy"),
                                               ("deploy", "preflight-cleanup", "preflight", "preflight"),
                                               ("smoke", "cleanup", "smoke", "smoke")):
            with self.subTest(phase=phase):
                original = rule(phase)
                self.context.update({f"needs.{producer}.outputs.resource_group": "rg",
                                     f"needs.{producer}.outputs.nsg_name": "nsg", f"needs.{producer}.result": "success",
                                     f"needs.{producer}.outputs.rule_name": original["name"]})
                self.data["rules"] = [original]
                self.data["gh"][f"/repos/{REPO}/actions/runs/10/attempts/1"] = dict(
                    id=10, run_attempt=1, status="completed", event="workflow_dispatch", head_branch="main",
                    path=f".github/workflows/{'smoke' if phase == 'smoke' else 'deploy'}.yml",
                    repository={"full_name": REPO}, head_repository={"full_name": REPO})
                step = next(s for s in self.workflows[workflow]["jobs"][job]["steps"] if s.get("name") == "Delete transient SSH rule")
                result = self.execute(step)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.data["rules"], [], f"attempt-1 SSH access remained: {result.stdout}")
                self.assertIn(f"NSG_ABSENT: {original['name']}", result.stdout)

                # The same caller must fail closed on active, unavailable, or
                # undeletable older ownership instead of reporting false absence.
                owner_path = f"/repos/{REPO}/actions/runs/10/attempts/1"
                owner = self.data["gh"][owner_path]
                for failure in ("active", "lookup", "delete", "missing", "future"):
                    with self.subTest(phase=phase, failure=failure):
                        self.data["rules"] = [original]
                        self.data["gh"][owner_path] = {**owner, "status": "in_progress" if failure == "active" else "completed"}
                        if failure == "lookup":
                            del self.data["gh"][owner_path]
                        self.data["az_fail"] = "delete" if failure == "delete" else ""
                        self.context[f"needs.{producer}.outputs.rule_name"] = (
                            "" if failure == "missing" else rule(phase, attempt=3)["name"] if failure == "future" else original["name"])
                        result = self.execute(step)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(self.data["rules"], [original])


if __name__ == "__main__":
    unittest.main()
