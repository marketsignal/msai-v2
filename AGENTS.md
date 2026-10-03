Read `docs/agent-context.md` completely before acting.

<!-- forge:begin v6 -->
<!-- forge-generated: true; canonical-path: .forge/instructions.md; canonical-revision: 8a7825003add32fdf2ccfdb4022ba6617309cac3bd2ef7b051afa58d132f69ad -->
Read the canonical Forge contract at `.forge/instructions.md` completely before taking project
action. This regular-file adapter does not duplicate that policy.
Keep Codex's native `/goal` and compose it over `.forge/workflows/goal.md`; never install or shadow
`.agents/skills/goal/SKILL.md`. Require the human-created Forge authorization record and authenticated
host qualification. On `FORGE_GOAL_BUDGET_EXHAUSTED`, checkpoint and stop. Treat
`FORGE_GOAL_STUCK_WARNING` as advisory. Resume the exact next durable step without resetting the
persistent Forge count, and pause for user input or any new external mutation.
Invoke the Forge opinion workflow with `$opinion`.
<!-- forge:end v6 -->
