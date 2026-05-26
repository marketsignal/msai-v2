"""Gate 3 — cross-file consistency lint for identity-contract.json.

For each canonical env-var declared in identity-contract.json#env_var_names,
walks the repo for references and asserts every literal value matches the
contract (or is a documented placeholder).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import jsonschema
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


# Dirs we never scan — generated, vendored, or our own contract.
# IMPORTANT: matched against path.relative_to(root).parts so this works
# both from the canonical repo root AND from a worktree under .worktrees/
# (where every absolute path contains .worktrees and would otherwise skip
# the entire repo).
EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "data",
    ".next",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    # NOTE: do NOT include ".worktrees" — the repo_root resolution finds
    # identity-contract.json in the worktree directly, so the walker scans
    # ONLY files inside that worktree's tree (relative paths). Adding
    # ".worktrees" would skip every file inside the worktree itself.
}
EXCLUDE_FILES = {
    "identity-contract.json",
    "identity-contract.schema.json",
    "identity-contract.README.md",
}

# A value matches the placeholder convention if it starts with any of these.
PLACEHOLDER_PREFIXES = ("your-", "<", "TBD", "EXAMPLE", "REPLACE", "placeholder")
# All-zero placeholder GUID — explicitly allowed (matches the convention
# already in .github/workflows/ci.yml).
ALL_ZERO_GUID = "00000000-0000-0000-0000-000000000000"

# Regexes for env-var references.
#
# IMPORTANT: use horizontal-only whitespace `[ \t]*` between separator and
# value (NOT `\s*` which matches newlines and would pull the next line's
# content into "value" on empty-value lines).
#
# Two distinct patterns:
#   1. ENV_LINE_RE — matches a line starting with `KEY=VALUE` or `KEY: VALUE`
#      (e.g., .env.example, docker-compose environment blocks)
#   2. DOCKER_E_FLAG_RE — matches Docker `-e KEY=VALUE` invocations in
#      workflow files (e.g., the existing .github/workflows/ci.yml)
#      Codex P2 from code-review iter 1.
ENV_LINE_RE = re.compile(
    # Match KEY=VALUE / KEY: VALUE lines across the canonical syntaxes:
    #   - .env.example map style:   KEY=value
    #   - compose map style:        KEY: value  /  KEY: "value"
    #   - compose list style:       - KEY=value
    #   - quoted YAML keys:         "KEY": value
    # Codex P2 iter 3 (list-form) + iter 4 (quoted-key) coverage.
    r"^[ \t]*-?[ \t]*(?P<q>['\"]?)(?P<key>[A-Z_][A-Z0-9_]+)(?P=q)"
    r"[ \t]*[:=][ \t]*[\"']?(?P<value>[^\"'\s#$]+)[\"']?",
    re.MULTILINE,
)
COMPOSE_QUOTED_LIST_RE = re.compile(
    # Compose accepts whole-entry-quoted list-form:
    #   - 'AZURE_TENANT_ID=value'
    #   - "AZURE_TENANT_ID=value"
    # ENV_LINE_RE's KEY-only quoting can't match this (closing quote is
    # after VALUE not after KEY). Codex P2 iter 4.
    r"^[ \t]*-[ \t]*(?P<wrapq>['\"])"
    r"(?P<key>[A-Z_][A-Z0-9_]+)=(?P<value>[^\s'\"$\\]+)"
    r"(?P=wrapq)",
    re.MULTILINE,
)
DOCKER_E_FLAG_RE = re.compile(
    # Accept all docker env-flag shapes:
    #   -e KEY=value
    #   --env KEY=value
    #   --env=KEY=value
    # plus optional matching quotes around the value. Codex P2 iter 2
    # (quoted values) + iter 4 (long-form flag).
    r"(?:-e|--env)(?:[ \t]+|=)"
    r"(?P<key>[A-Z_][A-Z0-9_]+)=(?P<openq>['\"]?)(?P<value>[^\s'\"$\\]+)(?P=openq)",
)


@dataclass
class Drift:
    file: Path
    line: int
    key: str
    actual: str
    expected: str

    def __str__(self) -> str:
        return (
            f"{self.file}:{self.line}: drift — "
            f"{self.key}={self.actual!r}, expected {self.expected!r}"
        )


# ---------------------------------------------------------------------------
# Repo discovery helpers.
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Resolve the repo root (the dir containing identity-contract.json)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "identity-contract.json").exists():
            return parent
    raise RuntimeError("identity-contract.json not found in any parent directory")


def _iter_target_files(root: Path) -> Iterator[Path]:
    """Yield all files Gate 3 inspects.

    Path-component matching uses relative-to-root parts so the walker
    works both from the canonical repo root AND from a worktree under
    `.worktrees/<name>/`.
    """
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in EXCLUDE_DIRS for part in rel_parts):
            continue
        if path.name in EXCLUDE_FILES:
            continue
        if path.name == ".env":
            continue  # gitignored real values
        if (
            path.name.endswith(".env.example")
            or path.name == ".env.example"
            or (
                path.suffix in (".yml", ".yaml")
                and ("docker-compose" in path.name or "workflows" in rel_parts)
            )
        ):
            yield path


# ---------------------------------------------------------------------------
# Test 1 — Schema validates the contract.
# ---------------------------------------------------------------------------


def test_identity_contract_matches_schema() -> None:
    root = _repo_root()
    schema = json.loads((root / "identity-contract.schema.json").read_text())
    contract = json.loads((root / "identity-contract.json").read_text())

    jsonschema.validate(contract, schema)


# ---------------------------------------------------------------------------
# Test 2 — Cross-file drift detection on the real repo.
# ---------------------------------------------------------------------------


def test_no_drift_across_repo_files() -> None:
    root = _repo_root()
    contract = json.loads((root / "identity-contract.json").read_text())
    expected = {
        env_var: contract["tenant_id"] for env_var in contract["env_var_names"]["tenant_id"]
    }
    expected.update(
        {env_var: contract["client_id"] for env_var in contract["env_var_names"]["client_id"]}
    )

    drifts: list[Drift] = []
    files_scanned = 0
    for file in _iter_target_files(root):
        files_scanned += 1
        text = file.read_text(errors="replace")
        # Three regex passes — line-leading KEY=VALUE, compose
        # whole-entry-quoted list-form, AND Docker -e/--env KEY=VALUE.
        # Codex P2 iters 1, 3, 4.
        for pattern in (ENV_LINE_RE, COMPOSE_QUOTED_LIST_RE, DOCKER_E_FLAG_RE):
            for match in pattern.finditer(text):
                key = match.group("key")
                value = match.group("value")
                if key not in expected:
                    continue
                if not value or value == ALL_ZERO_GUID:
                    continue
                if value.startswith(PLACEHOLDER_PREFIXES):
                    continue
                if value.startswith("$"):
                    continue
                if value != expected[key]:
                    line = text[: match.start()].count("\n") + 1
                    drifts.append(
                        Drift(
                            file=file,
                            line=line,
                            key=key,
                            actual=value,
                            expected=expected[key],
                        )
                    )

    # Defensive: if the walker scanned zero files something is wrong.
    assert files_scanned > 0, (
        f"Gate 3 file walker scanned zero files from root={root}. "
        "Verify _iter_target_files uses relative paths and EXCLUDE_DIRS "
        "does not include '.worktrees'."
    )

    if drifts:
        pytest.fail(
            "Identity contract drift detected:\n"
            + "\n".join(f"  {d}" for d in drifts)
            + "\n\nUpdate identity-contract.json OR the drifted file."
        )


# ---------------------------------------------------------------------------
# Test 3 — Drift IS detected when one is injected (the lint of the lint).
# ---------------------------------------------------------------------------


def test_drift_detection_works(tmp_path: Path) -> None:
    """Inject a deliberate drift in a fixture file and assert the lint catches it."""
    (tmp_path / "identity-contract.json").write_text(
        json.dumps(
            {
                "$schema": "./identity-contract.schema.json",
                "tenant_id": "11111111-1111-1111-1111-111111111111",
                "client_id": "22222222-2222-2222-2222-222222222222",
                "app_id_uri": "api://22222222-2222-2222-2222-222222222222",
                "issuer": (
                    "https://login.microsoftonline.com/11111111-1111-1111-1111-111111111111/v2.0"
                ),
                "scope_name": "access_as_user",
                "token_version": "2.0",
                "frontend_env_prefix": "NEXT_PUBLIC_",
                "env_var_names": {
                    "tenant_id": ["AZURE_TENANT_ID"],
                    "client_id": ["AZURE_CLIENT_ID"],
                },
            }
        )
    )
    # Drift: AZURE_TENANT_ID DIFFERS from the contract above.
    (tmp_path / ".env.example").write_text(
        "AZURE_TENANT_ID=99999999-9999-9999-9999-999999999999\n"
        "AZURE_CLIENT_ID=22222222-2222-2222-2222-222222222222\n"
    )

    contract = json.loads((tmp_path / "identity-contract.json").read_text())
    expected = {
        env_var: contract["tenant_id"] for env_var in contract["env_var_names"]["tenant_id"]
    }
    expected.update(
        {env_var: contract["client_id"] for env_var in contract["env_var_names"]["client_id"]}
    )

    drifts: list[tuple[Path, str, str, str]] = []
    for file in tmp_path.glob("*.env.example"):
        text = file.read_text()
        for match in ENV_LINE_RE.finditer(text):
            key = match.group("key")
            value = match.group("value")
            if key not in expected:
                continue
            if not value or value == ALL_ZERO_GUID:
                continue
            if value.startswith(PLACEHOLDER_PREFIXES) or value.startswith("$"):
                continue
            if value != expected[key]:
                drifts.append((file, key, value, expected[key]))

    assert len(drifts) == 1
    drift_file, drift_key, drift_actual, drift_expected = drifts[0]
    assert drift_key == "AZURE_TENANT_ID"
    assert drift_actual == "99999999-9999-9999-9999-999999999999"
    assert drift_expected == "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# Test 4 — Placeholder values are allowed.
# ---------------------------------------------------------------------------


def test_placeholder_values_in_env_example_are_allowed(tmp_path: Path) -> None:
    """`your-tenant-id`, `<GUID>`, `00000000-...` etc. should NOT be flagged."""
    (tmp_path / "fixture.env.example").write_text(
        "AZURE_TENANT_ID=your-tenant-id\n"
        "AZURE_CLIENT_ID=<GUID>\n"
        "JWT_TENANT_ID=00000000-0000-0000-0000-000000000000\n"
        "NEXT_PUBLIC_AZURE_TENANT_ID=TBD\n"
    )

    expected = {
        "AZURE_TENANT_ID": "real-tenant",
        "AZURE_CLIENT_ID": "real-client",
        "JWT_TENANT_ID": "real-tenant",
        "NEXT_PUBLIC_AZURE_TENANT_ID": "real-tenant",
    }

    drifts: list[tuple[str, str]] = []
    text = (tmp_path / "fixture.env.example").read_text()
    for match in ENV_LINE_RE.finditer(text):
        key = match.group("key")
        value = match.group("value")
        if key not in expected:
            continue
        if not value or value == ALL_ZERO_GUID:
            continue
        if value.startswith(PLACEHOLDER_PREFIXES) or value.startswith("$"):
            continue
        if value != expected[key]:
            drifts.append((key, value))

    assert drifts == []


# ---------------------------------------------------------------------------
# Test 5 — Docker -e KEY=VALUE shape is detected (Codex P2 iter 1).
# ---------------------------------------------------------------------------


def test_docker_e_flag_drift_is_detected(tmp_path: Path) -> None:
    """`-e KEY=VALUE` patterns in workflow YAMLs are scanned for drift."""
    workflow_yaml = (
        "          docker run --rm \\\n"
        "            -e AZURE_TENANT_ID=99999999-9999-9999-9999-999999999999 \\\n"
        "            -e AZURE_CLIENT_ID=88888888-8888-8888-8888-888888888888 \\\n"
        "            msai-backend:latest\n"
    )
    expected = {
        "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
    }

    drifts: list[tuple[str, str]] = []
    for match in DOCKER_E_FLAG_RE.finditer(workflow_yaml):
        key = match.group("key")
        value = match.group("value")
        if key not in expected:
            continue
        if not value or value == ALL_ZERO_GUID:
            continue
        if value.startswith(PLACEHOLDER_PREFIXES) or value.startswith("$"):
            continue
        if value != expected[key]:
            drifts.append((key, value))

    assert len(drifts) == 2
    assert {d[0] for d in drifts} == {"AZURE_TENANT_ID", "AZURE_CLIENT_ID"}


def test_docker_e_flag_quoted_value_drift_is_detected(tmp_path: Path) -> None:
    """`-e KEY="VALUE"` (double-quoted) and -e KEY='VALUE' (single-quoted) shapes
    are also detected. Codex P2 from code-review iter 2."""
    workflow_yaml = (
        "          docker run --rm \\\n"
        '            -e AZURE_TENANT_ID="99999999-9999-9999-9999-999999999999" \\\n'
        "            -e AZURE_CLIENT_ID='88888888-8888-8888-8888-888888888888' \\\n"
        "            msai-backend:latest\n"
    )
    expected = {
        "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
    }

    drifts: list[tuple[str, str]] = []
    for match in DOCKER_E_FLAG_RE.finditer(workflow_yaml):
        key = match.group("key")
        value = match.group("value")
        if key not in expected:
            continue
        if not value or value == ALL_ZERO_GUID:
            continue
        if value.startswith(PLACEHOLDER_PREFIXES) or value.startswith("$"):
            continue
        if value != expected[key]:
            drifts.append((key, value))

    assert len(drifts) == 2
    assert {d[0] for d in drifts} == {"AZURE_TENANT_ID", "AZURE_CLIENT_ID"}


def test_compose_list_form_drift_is_detected(tmp_path: Path) -> None:
    """docker-compose list-form `- KEY=VALUE` is also scanned. Codex P2 from
    code-review iter 3 — compose accepts both map-form and list-form for
    environment blocks; both shapes must be linted."""
    compose_yaml = (
        "services:\n"
        "  backend:\n"
        "    environment:\n"
        "      - AZURE_TENANT_ID=99999999-9999-9999-9999-999999999999\n"
        "      - AZURE_CLIENT_ID=88888888-8888-8888-8888-888888888888\n"
    )
    expected = {
        "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
    }

    drifts: list[tuple[str, str]] = []
    for match in ENV_LINE_RE.finditer(compose_yaml):
        key = match.group("key")
        value = match.group("value")
        if key not in expected:
            continue
        if not value or value == ALL_ZERO_GUID:
            continue
        if value.startswith(PLACEHOLDER_PREFIXES) or value.startswith("$"):
            continue
        if value != expected[key]:
            drifts.append((key, value))

    assert len(drifts) == 2
    assert {d[0] for d in drifts} == {"AZURE_TENANT_ID", "AZURE_CLIENT_ID"}


def test_compose_quoted_list_form_drift_is_detected(tmp_path: Path) -> None:
    """Compose accepts QUOTED list-form entries: `- "KEY=value"` and
    `- 'KEY=value'`. Caught by COMPOSE_QUOTED_LIST_RE.
    Codex P2 from code-review iter 4."""
    compose_yaml = (
        "services:\n"
        "  backend:\n"
        "    environment:\n"
        '      - "AZURE_TENANT_ID=99999999-9999-9999-9999-999999999999"\n'
        "      - 'AZURE_CLIENT_ID=88888888-8888-8888-8888-888888888888'\n"
    )
    expected = {
        "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
    }

    drifts: list[tuple[str, str]] = []
    for match in COMPOSE_QUOTED_LIST_RE.finditer(compose_yaml):
        key = match.group("key")
        value = match.group("value")
        if key not in expected:
            continue
        if not value or value == ALL_ZERO_GUID:
            continue
        if value.startswith(PLACEHOLDER_PREFIXES) or value.startswith("$"):
            continue
        if value != expected[key]:
            drifts.append((key, value))

    assert len(drifts) == 2
    assert {d[0] for d in drifts} == {"AZURE_TENANT_ID", "AZURE_CLIENT_ID"}


def test_docker_long_form_env_flag_drift_is_detected(tmp_path: Path) -> None:
    """Docker long-form --env KEY=value and --env=KEY=value are also scanned.
    Codex P2 from code-review iter 4."""
    workflow_yaml = (
        "          docker run --rm \\\n"
        "            --env AZURE_TENANT_ID=99999999-9999-9999-9999-999999999999 \\\n"
        "            --env=AZURE_CLIENT_ID=88888888-8888-8888-8888-888888888888 \\\n"
        "            msai-backend:latest\n"
    )
    expected = {
        "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
    }

    drifts: list[tuple[str, str]] = []
    for match in DOCKER_E_FLAG_RE.finditer(workflow_yaml):
        key = match.group("key")
        value = match.group("value")
        if key not in expected:
            continue
        if not value or value == ALL_ZERO_GUID:
            continue
        if value.startswith(PLACEHOLDER_PREFIXES) or value.startswith("$"):
            continue
        if value != expected[key]:
            drifts.append((key, value))

    assert len(drifts) == 2
    assert {d[0] for d in drifts} == {"AZURE_TENANT_ID", "AZURE_CLIENT_ID"}


def test_docker_e_flag_all_zero_placeholder_allowed(tmp_path: Path) -> None:
    """Existing ci.yml uses -e AZURE_TENANT_ID=00000000-... — must NOT drift."""
    workflow_yaml = (
        "          docker run --rm \\\n"
        "            -e AZURE_TENANT_ID=00000000-0000-0000-0000-000000000000 \\\n"
        "            -e AZURE_CLIENT_ID=00000000-0000-0000-0000-000000000000 \\\n"
        "            msai-backend:latest\n"
    )
    expected = {
        "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
    }

    drifts: list[tuple[str, str]] = []
    for match in DOCKER_E_FLAG_RE.finditer(workflow_yaml):
        key = match.group("key")
        value = match.group("value")
        if key not in expected:
            continue
        if not value or value == ALL_ZERO_GUID:
            continue
        if value.startswith(PLACEHOLDER_PREFIXES) or value.startswith("$"):
            continue
        if value != expected[key]:
            drifts.append((key, value))

    assert drifts == []
