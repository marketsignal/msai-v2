"""NSG side effects are exercised against a stateful Azure CLI boundary."""
import copy
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import venv
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
REPO = "marketsignal/msai-v2"
PHASES = {"deploy": (200, "", "deploy.yml"), "preflight": (201, "preflight-", "deploy.yml"), "smoke": (202, "smoke-", "smoke.yml")}


def azure_cli_fixture(folder, cli_code):
    """Install only disposable external packages; -I cannot use PYTHONPATH."""
    root = folder / "cli-venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    python = root / "bin/python"
    site = Path(subprocess.check_output(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
    requests = site / "requests"
    requests.mkdir()
    (requests / "structures.py").write_text('''import threading
assert threading.current_thread() is threading.main_thread()
MAIN_COMPLETE = True
''')
    (requests / "__init__.py").write_text('''from . import structures
import json, os
if os.environ.get("STARTUP_AUDIT"):
    with open(os.environ["STARTUP_AUDIT"], "a") as stream:
        stream.write(json.dumps({"event": "requests", "pid": os.getpid()}) + "\\n")
''')
    cli = site / "azure/cli"
    cli.mkdir(parents=True)
    (cli / "__init__.py").write_text("")
    (cli / "__main__.py").write_text('''import json, os, sys, threading
assert "requests.structures" in sys.modules, "requests preload missing before CLI entrypoint"
assert sys.modules["requests.structures"].MAIN_COMPLETE
assert threading.current_thread() is threading.main_thread()
if os.environ.get("STARTUP_AUDIT"):
    with open(os.environ["STARTUP_AUDIT"], "a") as stream:
        stream.write(json.dumps({"event": "cli", "pid": os.getpid(), "argv": sys.argv[1:],
                                 "installer": os.environ.get("AZ_INSTALLER"),
                                 "marker": os.environ.get("FIXTURE_MARKER"),
                                 "isolated": sys.flags.isolated}) + "\\n")
''' + cli_code)
    return python, requests


class AzureStartupTests(unittest.TestCase):
    """The real startup/helper run; only installed Azure/Requests are fixtures."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.audit = self.folder / "audit.jsonl"
        self.native = self.folder / "native.jsonl"
        self.python, self.requests = azure_cli_fixture(self.folder, '''
if sys.argv[1:] == ["--version"]:
    print("azure-cli 2.90.0\\ncore 2.90.0\\nPython (Linux) 3.14.6 (sensitive-payload)")
elif os.environ.get("FIXTURE_MODE") == "runtime-fail":
    print("sensitive-payload")
    print("Traceback (most recent call last):\\nValueError: sensitive-payload", file=sys.stderr)
    sys.exit(7)
elif os.environ.get("FIXTURE_MODE") == "exit":
    print("unchanged stdout")
    print("unchanged stderr", file=sys.stderr)
    sys.exit(23)
else:
    print(json.dumps({"args": sys.argv[1:], "installer": os.environ.get("AZ_INSTALLER"),
                      "marker": os.environ.get("FIXTURE_MARKER")}))
''')
        launcher = self.folder / "az"
        launcher.write_text(f"#!{sys.executable}\n" + '''import os, pathlib, sys
with pathlib.Path(os.environ["NATIVE_AUDIT"]).open("a") as stream:
    stream.write("called\\n")
print('{"native": true}')
sys.exit(int(os.environ.get("NATIVE_EXIT", "0")))
''')
        launcher.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{self.folder}:{os.environ['PATH']}",
                    "MSAI_AZURE_CLI_PYTHON": str(self.python), "AZ_INSTALLER": "DEB",
                    "FIXTURE_MARKER": "config-and-auth-preserved", "STARTUP_AUDIT": str(self.audit),
                    "NATIVE_AUDIT": str(self.native), "PYTHONDONTWRITEBYTECODE": "1"}
        self.startup = ROOT / "scripts/azure_cli_startup.py"
        self.args = ["create", "--resource-group", "fixture-rg", "--description", "two words"]

    def helper(self, **environment):
        # Call the real helper from a subprocess so launch/import errors cannot
        # be hidden by an in-process patch of its command selection.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import nsg_rule_lifecycle as n; "
                "print(n.az_json(sys.argv[2:]))")
        return subprocess.run([sys.executable, "-B", "-c", code, str(ROOT / "scripts"), *self.args],
                              env={**self.env, **environment}, text=True, capture_output=True, timeout=10)

    def events(self):
        return [json.loads(line) for line in self.audit.read_text().splitlines()] if self.audit.exists() else []

    def test_configured_operation_preloads_on_main_thread_in_same_child(self):
        result = self.helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("config-and-auth-preserved", result.stdout)
        events = self.events()
        self.assertEqual([e["event"] for e in events], ["requests", "cli"])
        self.assertEqual(events[0]["pid"], events[1]["pid"])
        self.assertEqual(events[1]["argv"], ["network", "nsg", "rule", *self.args, "--output", "json"])
        self.assertEqual(events[1]["installer"], "DEB")
        self.assertEqual(events[1]["isolated"], 1)
        self.assertFalse(self.native.exists(), "configured runtime fell back to native az")

    def test_startup_preserves_arguments_environment_output_and_system_exit(self):
        args = ["--literal", "two words", "--", "$"]
        result = subprocess.run([str(self.python), "-I", str(self.startup), *args],
                                env={**self.env, "FIXTURE_MODE": "exit"}, text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(result.stdout, "unchanged stdout\n")
        self.assertEqual(result.stderr, "unchanged stderr\n")
        self.assertEqual(self.events()[1]["argv"], args)
        self.assertEqual(self.events()[1]["marker"], "config-and-auth-preserved")

    def test_isolation_ignores_cwd_and_pythonpath_package_shadowing(self):
        poison = self.folder / "poison"
        poison.mkdir()
        (poison / "requests.py").write_text("raise RuntimeError('cwd/PYTHONPATH shadow imported')")
        (poison / "azure.py").write_text("raise RuntimeError('cwd/PYTHONPATH shadow imported')")
        result = subprocess.run([str(self.python), "-I", str(self.startup), "--version"],
                                cwd=poison, env={**self.env, "PYTHONPATH": str(poison)},
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("azure-cli 2.90.0", result.stdout)
        self.assertEqual([e["event"] for e in self.events()], ["requests", "cli"])

    def test_configured_runtime_failure_keeps_original_exit_and_redacted_same_runtime_versions(self):
        result = self.helper(FIXTURE_MODE="runtime-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Azure create failed (exit 7)", result.stderr)
        self.assertIn("runtime_cli=2.90.0 runtime_core=2.90.0 runtime_python=3.14.6", result.stderr)
        self.assertNotIn("sensitive-payload", result.stdout + result.stderr)
        entries = [e for e in self.events() if e["event"] == "cli"]
        self.assertEqual([e["argv"] for e in entries],
                         [["network", "nsg", "rule", *self.args, "--output", "json"], ["--version"]])
        self.assertTrue(all(e["installer"] == "DEB" and e["isolated"] == 1 for e in entries))
        self.assertFalse(self.native.exists())

    def test_explicit_invalid_runtime_never_falls_back(self):
        for value in ("", "relative/python", str(self.folder / "missing-python")):
            with self.subTest(value=value):
                result = self.helper(MSAI_AZURE_CLI_PYTHON=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("rule state is unproved", result.stderr)
                self.assertFalse(self.native.exists())
                self.assertEqual(self.events(), [])

    def test_preload_import_failure_never_enters_cli_or_native_launcher(self):
        (self.requests / "__init__.py").write_text("raise ImportError('sensitive-payload')")
        result = self.helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Azure create failed (exit 1)", result.stderr)
        self.assertNotIn("sensitive-payload", result.stdout + result.stderr)
        self.assertIn("runtime_cli=unknown runtime_core=unknown runtime_python=unknown", result.stderr)
        self.assertEqual(self.events(), [])
        self.assertFalse(self.native.exists())

    def test_truly_unset_runtime_retains_native_launcher(self):
        del self.env["MSAI_AZURE_CLI_PYTHON"]
        result = self.helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("'native': True", result.stdout)
        self.assertEqual(self.native.read_text(), "called\n")
        self.assertEqual(self.events(), [])

    def test_configured_operation_and_private_versions_keep_distinct_time_bounds(self):
        m = importlib.import_module("nsg_rule_lifecycle")
        failed = subprocess.CompletedProcess([], 7, "sensitive-payload",
                                             "Traceback (most recent call last):\nValueError: sensitive-payload")
        with patch.dict(os.environ, self.env), patch.object(m.subprocess, "run", side_effect=[
                failed, subprocess.TimeoutExpired("fixture", 5)]) as run:
            with self.assertRaises(m.Refusal) as caught:
                m.az_json(self.args)
        self.assertIn("Azure create failed (exit 7)", str(caught.exception))
        self.assertNotIn("sensitive-payload", str(caught.exception))
        self.assertEqual(run.call_count, 2)
        prefix = [str(self.python), "-I", str(self.startup)]
        self.assertEqual(run.call_args_list[0].args[0],
                         [*prefix, "network", "nsg", "rule", *self.args, "--output", "json"])
        self.assertEqual(run.call_args_list[1].args[0], [*prefix, "--version"])
        self.assertEqual([call.kwargs for call in run.call_args_list],
                         [dict(text=True, capture_output=True, timeout=120, check=False),
                          dict(text=True, capture_output=True, timeout=5, check=False)])


def rule(phase="deploy", run=10, attempt=1, legacy=False, **overrides):
    priority, prefix, workflow = PHASES[phase]
    desc = f"msai-ssh:v1;repo={REPO};workflow={workflow};run={run};attempt={attempt};phase={phase}"
    if legacy:
        source = {"deploy": "GHA", "preflight": "GHA deploy.yml preflight", "smoke": "GHA smoke.yml"}[phase]
        desc = f"Transient SSH from {source}. run_id={run} attempt={attempt}. See docs/decisions/deploy-ssh-jit.md"
    value = dict(name=f"gha-transient-{prefix}{run}-{attempt}", description=desc, priority=priority,
                 direction="Inbound", access="Allow", protocol="Tcp", sourceAddressPrefix="192.0.2.1/32",
                 sourcePortRange="*", destinationAddressPrefix="*", destinationPortRange="22")
    value.update(overrides)
    return value


class AzureBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module("nsg_rule_lifecycle")

    def refused(self, stderr, verb="list", returncode=1, runtime=False, version=None):
        args = [verb, "--resource-group", "rg", "--nsg-name", "nsg", "--subscription", "subscription-id"]
        response = subprocess.CompletedProcess([], returncode, "secret-stdout", stderr)
        version = version if version is not None else subprocess.CompletedProcess([], 0, "", "")
        with patch.object(self.m.subprocess, "run", side_effect=[response, version]) as run:
            with self.assertRaises(self.m.Refusal) as caught:
                self.m.az_json(args)
        # Diagnostics must not alter the command, retry it, or expose stdout.
        self.assertEqual(run.call_count, 2 if runtime else 1)
        self.assertEqual(run.call_args_list[0].args[0],
                         ["az", "network", "nsg", "rule", *args, "--output", "json"])
        self.assertEqual(run.call_args_list[0].kwargs,
                         dict(text=True, capture_output=True, timeout=120, check=False))
        if runtime:
            self.assertEqual(run.call_args_list[1].args[0], ["az", "--version"])
            self.assertEqual(run.call_args_list[1].kwargs,
                             dict(text=True, capture_output=True, timeout=5, check=False))
        message = str(caught.exception)
        self.assertIn(f"Azure {verb} failed (exit {returncode})", message)
        self.assertNotIn("secret-stdout", message)
        self.assertNotIn("sensitive-payload", message)
        return message

    def test_standard_azure_codes_are_visible_without_message_bodies(self):
        for verb, code, stderr in (
            ("create", "AuthorizationFailed", "ERROR: (AuthorizationFailed) sensitive-payload\n"),
            ("delete", "ScopeLocked", "WARNING: sensitive-payload\nERROR: (ScopeLocked) sensitive-payload\n"),
            ("list", "ResourceNotFound", "ERROR: sensitive-payload\nCode: ResourceNotFound\nMessage: sensitive-payload\n"),
        ):
            with self.subTest(verb=verb):
                self.assertIn(f"code={code}", self.refused(stderr, verb))

    def test_cli_categories_withhold_sensitive_diagnostics(self):
        for stderr, category in (
            ("ERROR: unrecognized arguments: sensitive-payload", "CLI_ARGUMENT_ERROR"),
            ("ERROR: the following arguments are required: sensitive-payload", "CLI_ARGUMENT_ERROR"),
            ("ERROR: argument --access: invalid choice: sensitive-payload", "CLI_ARGUMENT_ERROR"),
            ("ERROR: Please run 'az login' to setup account. sensitive-payload", "AUTHENTICATION_REQUIRED"),
            ("ERROR: The subscription of 'sensitive-payload' doesn't exist in cloud 'AzureCloud'.", "SUBSCRIPTION_SELECTION"),
            ("ERROR: No subscriptions found for sensitive-payload", "SUBSCRIPTION_SELECTION"),
            ("ERROR: requests.exceptions.ConnectionError: sensitive-payload", "CONNECTION_OR_TLS"),
            ("ERROR: SSLError: certificate verify failed: sensitive-payload", "CONNECTION_OR_TLS"),
            ("Traceback (most recent call last):\n  sensitive-payload\nAttributeError: sensitive-payload", "CLI_RUNTIME_ERROR"),
        ):
            with self.subTest(category=category, stderr=stderr):
                self.assertIn(f"category={category}", self.refused(
                    stderr, returncode=2, runtime=category == "CLI_RUNTIME_ERROR"))

    def test_runtime_signature_requires_traceback_and_exact_terminal_exception(self) -> None:
        terminal = ("_frozen_importlib._DeadlockError: deadlock detected by "
                    "_ModuleLock('requests.structures') at 123456")
        trace = "Traceback (most recent call last):\n  sensitive-payload\n" + terminal
        message = self.refused(trace, "create", runtime=True)
        self.assertIn("category=CLI_RUNTIME_ERROR signature=REQUESTS_STRUCTURES_IMPORT_DEADLOCK", message)
        for changed in (trace.replace("requests.structures", "requests.sessions"),
                        trace.replace("_DeadlockError", "RuntimeError"),
                        trace.replace("Traceback (", "quoted Traceback ("),
                        trace + "\nValueError: sensitive-payload",
                        trace.replace(terminal, "  " + terminal),
                        trace.replace("at 123456", "at sensitive-payload")):
            with self.subTest(trace=changed):
                self.assertNotIn("signature=", self.refused(changed, runtime=True))
        self.assertNotIn("signature=", self.refused(terminal))
        # ARM codes retain precedence, even if surrounded by a runtime trace.
        self.assertIn("code=AuthorizationFailed", self.refused(trace + "\nCode: AuthorizationFailed"))

    def test_runtime_versions_expose_only_anchored_numeric_fields(self) -> None:
        trace = "Traceback (most recent call last):\nValueError: sensitive-payload"
        output = ("azure-cli                         2.90.0 *\ncore                              2.90.0\n"
                  "telemetry                          1.1.0\nExtensions directory 'sensitive-payload'\n"
                  "Python location 'sensitive-payload'\n"
                  "Python (Linux) 3.14.6 (main, sensitive-payload) [GCC sensitive-payload]\n")
        message = self.refused(trace, "delete", runtime=True,
                               version=subprocess.CompletedProcess([], 0, output, "sensitive-payload"))
        self.assertTrue(message.endswith("runtime_cli=2.90.0 runtime_core=2.90.0 runtime_python=3.14.6"))
        for bad, expected in (
            (output.replace("2.90.0 *", "2.90.0-sensitive-payload"), "runtime_cli=unknown"),
            (output.replace("2.90.0 *", "12345.90.0"), "runtime_cli=unknown"),
            (output.replace("3.14.6 (", "3.14.6.1 ("), "runtime_python=unknown"),
            (output.replace("3.14.6 (", "3.14.6sensitive-payload ("), "runtime_python=unknown"),
            (output + "azure-cli 2.90.0\n", "runtime_cli=unknown"),
            (output + "core sensitive-payload\n", "runtime_core=unknown"),
            (output + "Python (Linux) 3.14.6\n", "runtime_python=unknown"),
            ("secret-stdout\nsensitive-payload\n", "runtime_cli=unknown runtime_core=unknown runtime_python=unknown"),
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.refused(trace, runtime=True,
                              version=subprocess.CompletedProcess([], 0, bad, "sensitive-payload")))

    def test_runtime_signature_accepts_only_the_exact_azure_cli_help_footer(self) -> None:
        # Azure CLI 2.90.0 logs the unexpected-error traceback, then prints this
        # fixed recommendation separately to stderr (azclierror.print_error).
        trace = (
            "ERROR: The command failed with an unexpected error. Here is the traceback:\n"
            "ERROR: deadlock detected by _ModuleLock('requests.structures') at 123456\n"
            "Traceback (most recent call last):\n"
            '  File "/sensitive-payload/azure/cli/core/util.py", line 145, in handle_exception\n'
            "    sensitive-payload\n"
            "_frozen_importlib._DeadlockError: deadlock detected by "
            "_ModuleLock('requests.structures') at 123456\n"
        )
        footer = "To check existing issues, please visit: https://github.com/Azure/azure-cli/issues\n"
        for stderr in (trace, trace + footer, trace + "\n" + footer):
            with self.subTest(stderr=stderr):
                message = self.refused(stderr, "create", runtime=True)
                self.assertIn("signature=REQUESTS_STRUCTURES_IMPORT_DEADLOCK", message)
                self.assertNotIn("To check existing issues", message)
                self.assertNotIn("Traceback", message)
        for suffix in (
            "sensitive-payload\n", "ValueError: sensitive-payload\n" + footer,
            footer + "sensitive-payload\n", footer + "ValueError: sensitive-payload\n",
            footer.replace("azure-cli/issues", "sensitive-payload"),
            footer.rstrip() + " sensitive-payload\n", footer + footer,
        ):
            with self.subTest(suffix=suffix):
                self.assertNotIn("signature=", self.refused(trace + suffix, "create", runtime=True))

    def test_runtime_version_failure_preserves_original_refusal_without_retry(self) -> None:
        trace = "Traceback (most recent call last):\nValueError: sensitive-payload"
        for version in (subprocess.CompletedProcess([], 8, "azure-cli 2.90.0\n", "sensitive-payload"),
                        subprocess.TimeoutExpired("sensitive-payload", 5, stderr="sensitive-payload"),
                        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "sensitive-payload"),
                        OSError("sensitive-payload")):
            with self.subTest(failure=type(version).__name__):
                message = self.refused(trace, "create", returncode=2, runtime=True, version=version)
                self.assertTrue(message.endswith("runtime_cli=unknown runtime_core=unknown runtime_python=unknown"))

    def test_runtime_refusal_main_exits_one_without_success_output(self) -> None:
        argv = ["nsg_rule_lifecycle.py", "reap", "--dry-run", "--resource-group", "rg", "--nsg-name", "nsg",
                "--subscription", "subscription-id", "--repository", REPO]
        responses = [subprocess.CompletedProcess([], 2, "sensitive-payload",
                     "Traceback (most recent call last):\nValueError: sensitive-payload"),
                     subprocess.CompletedProcess([], 0, "azure-cli 2.90.0\ncore 2.90.0 *\nPython (Linux) 3.14.6\n",
                                                 "sensitive-payload")]
        with patch.object(sys, "argv", argv), patch.object(sys, "stderr", new_callable=io.StringIO) as stderr, \
                patch.object(sys, "stdout", new_callable=io.StringIO) as stdout, \
                patch.object(self.m.subprocess, "run", side_effect=responses) as run:
            self.assertEqual(self.m.main(), 1)
        self.assertIn("NSG_REFUSED: Azure list failed (exit 2)", stderr.getvalue())
        self.assertIn("runtime_cli=2.90.0 runtime_core=2.90.0 runtime_python=3.14.6", stderr.getvalue())
        self.assertNotIn("sensitive-payload", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(run.call_count, 2)

    def test_unknown_and_empty_errors_are_explicitly_unclassified(self):
        for stderr in ("", "ERROR: sensitive-payload", "token=sensitive-payload\nBearer sensitive-payload\n"):
            with self.subTest(stderr=stderr):
                self.assertIn("category=UNCLASSIFIED (details withheld)", self.refused(stderr))

    def test_code_extraction_is_anchored_bounded_and_symbolic(self):
        for stderr in (
            "message ERROR: (AuthorizationFailed) sensitive-payload",
            "Code: ResourceNotFound sensitive-payload",
            "ERROR: (sensitive-payload) hidden",
            "ERROR: (A" + "B" * 64 + ") hidden",
            "Code: A" + "B" * 64,
            "ERROR: (1Invalid) hidden",
            "ERROR: (Überraschung) hidden",
            "ERROR: (ResourceNotFound\nsensitive-payload) hidden",
        ):
            with self.subTest(stderr=stderr):
                self.assertIn("category=UNCLASSIFIED (details withheld)", self.refused(stderr))
        self.assertIn("code=" + "A" * 64, self.refused("Code: " + "A" * 64 + "\n"))

    def test_success_retains_json_and_empty_delete_results(self):
        for verb, stdout, wanted in (("list", '[{"name": "operator"}]', [{"name": "operator"}]),
                                    ("create", '{"name": "temporary"}', {"name": "temporary"}),
                                    ("delete", " \n", None)):
            with self.subTest(verb=verb), patch.object(self.m.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, stdout, "ERROR: (IgnoredWarning) sensitive-payload")) as run:
                self.assertEqual(self.m.az_json([verb]), wanted)
                self.assertEqual(run.call_count, 1)

    def test_malformed_json_still_refuses_without_echoing_it(self):
        with patch.object(self.m.subprocess, "run", return_value=
                subprocess.CompletedProcess([], 0, "sensitive-payload", "")):
            with self.assertRaisesRegex(self.m.Refusal, "^Azure list returned malformed JSON$"):
                self.m.az_json(["list"])

    def test_timeouts_and_missing_cli_still_refuse_without_diagnostics(self):
        for error in (subprocess.TimeoutExpired("sensitive-payload", 120, stderr="sensitive-payload"),
                      OSError("sensitive-payload")):
            with self.subTest(error=type(error).__name__), patch.object(self.m.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(self.m.Refusal, "^Azure list unavailable/timed out; rule state is unproved$"):
                    self.m.az_json(["list"])

    def test_failed_preview_exits_one_without_success_or_followup_calls(self):
        argv = ["nsg_rule_lifecycle.py", "reap", "--dry-run", "--resource-group", "rg", "--nsg-name", "nsg",
                "--subscription", "subscription-id", "--repository", REPO]
        with patch.object(sys, "argv", argv), patch.object(sys, "stderr", new_callable=io.StringIO) as stderr, \
                patch.object(sys, "stdout", new_callable=io.StringIO) as stdout, \
                patch.object(self.m.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 1, "", "ERROR: (ResourceNotFound) sensitive-payload")) as run:
            self.assertEqual(self.m.main(), 1)
        self.assertIn("NSG_REFUSED:", stderr.getvalue())
        self.assertIn("code=ResourceNotFound", stderr.getvalue())
        self.assertNotIn("sensitive-payload", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(run.call_count, 1)


class NSGTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "scripts/nsg_rule_lifecycle.py").exists(), "NSG lifecycle helper is missing")
        self.m = importlib.import_module("nsg_rule_lifecycle")
        self.rules = []
        self.calls = []
        self.status = "completed"
        self.run_override = {}
        self.fail = None
        self.keep_deleted = False
        self.current_phase = "deploy"
        self.az_patch = patch.object(self.m, "az_json", self.azure)
        self.gh_patch = patch.object(self.m, "gh_json", self.github)
        self.az_patch.start()
        self.gh_patch.start()
        self.addCleanup(self.az_patch.stop)
        self.addCleanup(self.gh_patch.stop)

    def azure(self, args):
        self.assertIn("--subscription", args)
        self.assertEqual(args[args.index("--subscription") + 1], "subscription-id")
        verb = args[0]
        self.calls.append((verb, list(args)))
        if self.fail == verb:
            raise self.m.Refusal(f"Azure {verb} failed")
        if verb == "list":
            return copy.deepcopy(self.rules)
        name = args[args.index("--name") + 1]
        if verb == "delete":
            if not self.keep_deleted:
                self.rules = [r for r in self.rules if r["name"] != name]
            return None
        if verb == "create":
            self.rules.append(dict(name=name))
            return None
        raise AssertionError(verb)

    def github(self, endpoint):
        self.calls.append(("github", endpoint))
        if self.fail == "github":
            raise self.m.Refusal("GitHub read failed")
        parts = endpoint.split("/")
        run_id = int(parts[parts.index("runs") + 1])
        attempt = int(parts[parts.index("attempts") + 1])
        phase = "smoke" if any(r["name"] == f"gha-transient-smoke-{run_id}-{attempt}" for r in self.rules) else "deploy"
        if run_id == 20 and self.current_phase == "smoke":
            phase = "smoke"
        return {**dict(id=run_id, run_attempt=attempt, status=self.status, event="workflow_dispatch",
                       path=f".github/workflows/{phase}.yml", head_branch="main",
                       repository={"full_name": REPO}, head_repository={"full_name": REPO}), **self.run_override}

    def create(self, phase="deploy"):
        self.current_phase = phase
        self.m.create("rg", "nsg", REPO, phase, 20, 1, "192.0.2.2", "subscription-id")

    def cleanup(self, phase="deploy", result="success", caller_attempt=1, producer_rule_name=None):
        if producer_rule_name is None:
            producer_rule_name = rule(phase)["name"]
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "10", "GITHUB_RUN_ATTEMPT": str(caller_attempt)}):
            self.m.cleanup("rg", "nsg", REPO, phase, 10, caller_attempt, result, "subscription-id", producer_rule_name)

    def test_completed_owned_conflict_is_rechecked_deleted_absent_then_created(self):
        for phase in PHASES:
            with self.subTest(phase=phase):
                self.rules = [rule(phase), {"name": "operator", "priority": 100, "direction": "Inbound"}]
                self.calls = []
                self.create(phase)
                verbs = [v for v, _ in self.calls]
                delete = verbs.index("delete")
                create = verbs.index("create")
                self.assertIn("list", verbs[:delete])
                self.assertGreaterEqual(verbs[:delete].count("github"), 2)
                self.assertIn("list", verbs[delete + 1:create])
                self.assertEqual([r["name"] for r in self.rules], ["operator", f"gha-transient-{PHASES[phase][1]}20-1"])

    def test_active_old_owner_and_unknown_owner_are_preserved(self):
        for bad in (rule(), rule(description=""), rule(description="other"), rule(name="gha-transient-unowned")):
            self.rules = [bad]
            self.status = "in_progress"
            with self.assertRaises(self.m.Refusal):
                self.create()
            self.assertEqual(self.rules, [bad])
        self.assertFalse(any(v == "delete" for v, _ in self.calls))

    def test_exact_legacy_forms_allowed_only_with_completed_run_proof(self):
        for phase in PHASES:
            self.rules = [rule(phase, legacy=True)]
            self.create(phase)
            self.assertEqual(len(self.rules), 1)
        self.rules = [rule(legacy=True)]
        self.fail = "github"
        with self.assertRaises(self.m.Refusal):
            self.create()
        self.assertEqual(self.rules, [rule(legacy=True)])

    def test_shape_and_ownership_mismatch_never_deleted(self):
        for field, bad in (("access", "Deny"), ("protocol", "*"), ("direction", "Outbound"),
                           ("sourceAddressPrefix", "*"), ("sourceAddressPrefix", "192.0.2.1/24"),
                           ("destinationPortRange", "22-23"), ("sourcePortRange", "22"),
                           ("destinationAddressPrefix", "192.0.2.3"),
                           ("description", rule()["description"].replace(REPO, "other/repo")),
                           ("destinationPortRanges", ["22", "80"]),
                           ("sourceApplicationSecurityGroups", [{"id": "anything"}])):
            item = rule(**{field: bad})
            self.rules = [item]
            self.calls = []
            if field == "direction":
                self.m.reap("rg", "nsg", REPO, "subscription-id")
            else:
                with self.subTest(field=field), self.assertRaises(self.m.Refusal):
                    self.create()
            self.assertEqual(self.rules, [item])
            self.assertFalse(any(v == "delete" for v, _ in self.calls))

    def test_current_cleanup_all_three_phases_can_finish_while_attempt_running(self):
        self.status = "in_progress"
        for phase in PHASES:
            self.rules = [rule(phase)]
            self.cleanup(phase)
            self.assertEqual(self.rules, [])
        self.rules = [rule()]
        with self.assertRaises(self.m.Refusal):
            self.cleanup(result="running")
        self.assertEqual(self.rules, [rule()])

    def test_already_absent_requires_successful_read_and_delete_failures_propagate(self):
        self.cleanup()
        self.assertEqual([v for v, _ in self.calls], ["list"])
        for failure in ("list", "delete", "github"):
            self.rules = [rule()]
            self.fail = failure
            with self.subTest(failure=failure), self.assertRaises(self.m.Refusal):
                self.cleanup()
            self.assertEqual(self.rules, [rule()])
        self.fail = None
        self.keep_deleted = True
        with self.assertRaises(self.m.Refusal):
            self.cleanup()

    def test_reaper_preserves_unrelated_active_unknown_and_deletes_only_completed(self):
        unrelated = {"name": "operator", "priority": 100}
        unknown = rule(run=11, description="unknown")
        self.rules = [rule(), unrelated, unknown]
        self.m.reap("rg", "nsg", REPO, "subscription-id")
        self.assertEqual(self.rules, [unrelated, unknown])
        self.rules = [rule()]
        self.status = "in_progress"
        self.m.reap("rg", "nsg", REPO, "subscription-id")
        self.assertEqual(self.rules, [rule()])
        self.fail = "github"
        with self.assertRaises(self.m.Refusal):
            self.m.reap("rg", "nsg", REPO, "subscription-id")
        self.assertEqual(self.rules, [rule()])

    def test_preview_never_mutates(self):
        self.rules = [rule()]
        self.m.reap("rg", "nsg", REPO, "subscription-id", dry_run=True)
        self.assertEqual(self.rules, [rule()])
        self.assertFalse(any(v in ("create", "delete") for v, _ in self.calls))

    def test_wrong_github_owner_identity_preserves_rule(self):
        for field, value in (("id", 11), ("run_attempt", 2), ("run_attempt", True),
                             ("path", ".github/workflows/unrelated.yml"), ("head_branch", "feature"),
                             ("event", "pull_request"), ("repository", {"full_name": "other/repo"}),
                             ("head_repository", None), ("status", "unknown")):
            self.rules = [rule()]
            self.run_override = {field: value}
            self.calls = []
            with self.subTest(field=field), self.assertRaises(self.m.Refusal):
                self.create()
            self.assertEqual(self.rules, [rule()])
            self.assertFalse(any(v == "delete" for v, _ in self.calls))

    def test_old_completed_attempt_cleanup_does_not_read_newer_active_attempt(self):
        self.rules = [rule(attempt=1)]
        self.create()
        paths = [c for verb, c in self.calls if verb == "github"]
        self.assertTrue(all(c.endswith("/runs/10/attempts/1") for c in paths if "/runs/10/" in c))
        self.assertTrue(any(c.endswith("/runs/10/attempts/1") for c in paths))

    def test_rule_or_owner_change_before_delete_refuses(self):
        for change in ("owner", "rule"):
            self.rules = [rule()]
            reads = 0
            original = self.github
            def github(endpoint):
                nonlocal reads
                reads += 1
                if reads == 1:
                    if change == "owner":
                        result = original(endpoint)
                        self.status = "in_progress"
                        return result
                    self.rules[0]["destinationPortRange"] = "80"
                return original(endpoint)
            with patch.object(self.m, "gh_json", github), self.subTest(change=change), self.assertRaises(self.m.Refusal):
                self.create()
            self.assertTrue(self.rules)
            self.status = "completed"

    def test_singular_and_plural_shape_is_supported_without_widening(self):
        self.rules = [rule(sourceAddressPrefix=None, sourceAddressPrefixes=["192.0.2.1/32"],
                           sourcePortRange=None, sourcePortRanges=["*"],
                           destinationAddressPrefix=None, destinationAddressPrefixes=["*"],
                           destinationPortRange=None, destinationPortRanges=["22"])]
        self.create()
        self.assertEqual([r["name"] for r in self.rules], ["gha-transient-20-1"])

    def test_cleanup_rejects_another_current_attempt(self):
        self.rules = [rule()]
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "10", "GITHUB_RUN_ATTEMPT": "2"}):
            with self.assertRaises(self.m.Refusal):
                self.m.cleanup("rg", "nsg", REPO, "deploy", 10, 1, "success", "subscription-id", "gha-transient-10-1")
        self.assertEqual(self.rules, [rule()])

    def test_creation_refuses_unprovable_new_owner_before_mutating_even_empty_nsg(self):
        self.run_override = {"head_branch": "feature"}
        with self.assertRaises(self.m.Refusal):
            self.create()
        self.assertFalse(any(v in ("delete", "create") for v, _ in self.calls))

    def test_cleanup_rerun_requires_completed_producer_attempt_and_preserves_other_rules(self):
        for phase in PHASES:
            for status in ("completed", "in_progress", "queued", "unknown"):
                original = rule(phase)
                newer = rule(phase, attempt=2)
                self.rules = [original, newer]
                self.status = status
                self.calls = []
                with self.subTest(phase=phase, status=status):
                    if status == "completed":
                        self.cleanup(phase, caller_attempt=2)
                        self.assertEqual(self.rules, [newer])
                        paths = [c for verb, c in self.calls if verb == "github"]
                        self.assertTrue(paths)
                        self.assertTrue(all(c.endswith("/runs/10/attempts/1") for c in paths))
                    else:
                        with self.assertRaises(self.m.Refusal):
                            self.cleanup(phase, caller_attempt=2)
                        self.assertEqual(self.rules, [original, newer])
                        self.assertFalse(any(v == "delete" for v, _ in self.calls))

    def test_cleanup_rejects_missing_mismatched_or_future_producer_identity_before_azure(self):
        for name in ("", "gha-transient-10-0", "gha-transient-10-3", "gha-transient-11-1",
                     "gha-transient-smoke-10-1", "gha-transient-10-01", "gha-transient-10-1-extra"):
            self.rules = [rule()]
            self.calls = []
            with self.subTest(name=name), self.assertRaises(self.m.Refusal):
                self.cleanup(caller_attempt=2, producer_rule_name=name)
            self.assertEqual(self.rules, [rule()])
            self.assertEqual(self.calls, [])

    def test_cleanup_rerun_lookup_delete_and_shape_failures_preserve_producer_rule(self):
        for failure in ("github", "delete", "shape", "attempt"):
            original = rule(description="unproved") if failure == "shape" else rule()
            self.rules = [original]
            self.fail = failure if failure in ("github", "delete") else None
            self.run_override = {"run_attempt": 2} if failure == "attempt" else {}
            with self.subTest(failure=failure), self.assertRaises(self.m.Refusal):
                self.cleanup(caller_attempt=2)
            self.assertEqual(self.rules, [original])

    def test_cleanup_rerun_rechecks_completed_owner_immediately_before_deleting(self):
        self.rules = [rule()]
        reads = 0
        original = self.github
        def become_active(endpoint):
            nonlocal reads
            reads += 1
            if reads > 1:
                self.status = "in_progress"
            return original(endpoint)
        with patch.object(self.m, "gh_json", become_active), self.assertRaises(self.m.Refusal):
            self.cleanup(caller_attempt=2)
        self.assertEqual(self.rules, [rule()])
        self.assertFalse(any(v == "delete" for v, _ in self.calls))


if __name__ == "__main__":
    unittest.main()
