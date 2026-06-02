# UC-BA-CLI-1 — Operator adds a broker account from the shell and lists it back

**Interface:** CLI
**Priority:** P1
**Status:** GRADUATED
**Last Result:** PASS (2026-06-02, feature run)

**Actor:** Operator on the box bootstrapping a new account from the CLI.

**Scenario:** They prefer the shell for ops work and want to add an account and confirm it
landed in the fleet registry without opening the UI.

**Intent:** The operator adds a broker account and lists it back in a separate invocation to
confirm it persists, with no secret echoed to the terminal.

**Setup:** `MSAI_API_KEY` available in the backend container env; backend reachable. The
password is supplied via `MSAI_BROKER_TWS_PASSWORD` (never argv). Do NOT pre-create the account.

**Invocation note:** the `msai` console script is not installed in the dev image
(`uv sync --no-install-project`); the working sanctioned form is the module entry:
`docker exec -w /app -e MSAI_BROKER_TWS_PASSWORD=<pw> msai-claude-backend uv run python -m msai.cli broker <cmd>` (API reached at the container's internal `localhost:8000`).

**Steps:**

1. `... broker add --ib-account-id DU<digits> --ib-login-key <login> --trading-mode paper --tws-userid <u>` (password from `$MSAI_BROKER_TWS_PASSWORD`).
2. `... broker list` (separate invocation).

**Verification:** `add` stdout shows `Created broker account DU<digits> (id: <uuid>, status:
active, slot: <slot>)`, exit 0, and **no credential value** in stdout (there is intentionally
NO `--tws-password` flag). The next `broker list` invocation returns a table whose rows include
the new account with its status + slot — **no secret**.

**Persistence:** A fresh `broker list` invocation still lists the account with the same id;
`broker show <id>` echoes credential metadata only, never a secret.
