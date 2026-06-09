"use client";

/**
 * PortfolioStartDialog — registry-backed deploy flow for a frozen
 * LivePortfolioRevision (PR4 rewrite).
 *
 *   Stage 1: Form — pick an EXPLICIT target account from the registered
 *            broker-account registry (shadcn Select). NO free-text account-id,
 *            NO browser prefix-parse (PRD §6 — never infer real-money from a
 *            string). Pre-filled from the global account selector ONLY when
 *            that scope is a single concrete account; "all"/"unassigned"
 *            leave it unset and DISABLE Deploy with a "pick a target" notice
 *            (US-002). `paper_trading` is DERIVED from the chosen account's
 *            trading_mode.
 *   Stage 2: Preview (GET revision members).
 *   Stage 3: Real-money confirm — shown ONLY when the chosen account is the
 *            production fund (`is_real_money`). The operator types the exact
 *            `ib_account_id`; that value is sent as `confirm_account_id` so the
 *            SERVER is the authority (test/paper accounts skip this stage).
 *   Stage 4: Submit (POST /api/v1/live/start-portfolio with Idempotency-Key,
 *            `broker_account_id` + `selector_context_account_id`).
 *
 * 422 envelopes are decoded inline:
 *   - BINDING_MISMATCH            → mismatches table
 *   - LIVE_DEPLOY_CONFLICT        → remediation callout (no retry CTA)
 *   - REAL_MONEY_CONFIRM_REQUIRED → confirm-stage callout (server authority)
 *   - REAL_MONEY_CONFIRM_MISMATCH → confirm-stage callout (server authority)
 *   - AMBIGUOUS_DEPLOY_TARGET     → callout (should be unreachable from the UI
 *                                   happy path — the dialog sends exactly one
 *                                   target — but the server is authoritative)
 *   - other 422 codes             → red callout with body.error.message
 *
 * Accepts HTTP 200 OR 201 as success (warm-restart vs cold). `startPortfolio`
 * throws ApiError on non-2xx, so both reach the success path naturally.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useAuth } from "@/lib/auth";
import { useAccountScope } from "@/lib/account-scope";
import {
  ApiError,
  describeApiError,
  getRevisionMembers,
  startPortfolio,
} from "@/lib/api";
import type {
  LivePortfolioMemberFrozen,
  LivePortfolioRevision,
  PortfolioStartResponse,
} from "@/lib/api";
import {
  listBrokerAccounts,
  type BrokerAccount,
} from "@/lib/api/broker-accounts";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface PortfolioStartDialogProps {
  revision: LivePortfolioRevision;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess?: (result: PortfolioStartResponse) => void;
}

type Stage = "form" | "preview" | "confirm" | "submitting";

// ---------------------------------------------------------------------------
// 422 envelope shapes + type guards
// ---------------------------------------------------------------------------

interface BindingMismatchEntry {
  field: string;
  member_value: unknown;
  candidate_value: unknown;
}

interface BindingMismatchEnvelope {
  error: {
    code: "BINDING_MISMATCH";
    message?: string;
    details: { mismatches: BindingMismatchEntry[] };
  };
}

interface LiveDeployConflictEnvelope {
  error: {
    code: "LIVE_DEPLOY_CONFLICT";
    message?: string;
    details: { existing_deployment_id: string; status?: string };
  };
}

interface GenericErrorEnvelope {
  error: { code?: string; message?: string };
}

function unwrapError(body: unknown): unknown {
  // Backend may wrap as { error: ... } (top-level) or { detail: { error: ... } }
  // (legacy HTTPException). Normalize to the inner object.
  if (!body || typeof body !== "object") return null;
  const bag = body as { error?: unknown; detail?: unknown };
  if (bag.error && typeof bag.error === "object") return body;
  const detail = bag.detail;
  if (detail && typeof detail === "object") {
    const inner = (detail as { error?: unknown }).error;
    if (inner && typeof inner === "object") return { error: inner };
  }
  return null;
}

function isBindingMismatch(body: unknown): body is BindingMismatchEnvelope {
  const normalized = unwrapError(body);
  if (!normalized) return false;
  const err = (normalized as { error: { code?: unknown; details?: unknown } })
    .error;
  if (err.code !== "BINDING_MISMATCH") return false;
  const details = err.details as { mismatches?: unknown } | undefined;
  if (!details || !Array.isArray(details.mismatches)) return false;
  return details.mismatches.every(
    (m) =>
      m != null &&
      typeof m === "object" &&
      typeof (m as { field?: unknown }).field === "string",
  );
}

function isLiveDeployConflict(
  body: unknown,
): body is LiveDeployConflictEnvelope {
  const normalized = unwrapError(body);
  if (!normalized) return false;
  const err = (normalized as { error: { code?: unknown; details?: unknown } })
    .error;
  if (err.code !== "LIVE_DEPLOY_CONFLICT") return false;
  const details = err.details as
    | { existing_deployment_id?: unknown }
    | undefined;
  return !!details && typeof details.existing_deployment_id === "string";
}

function errorCode(body: unknown): string | null {
  const normalized = unwrapError(body);
  if (!normalized) return null;
  const err = (normalized as GenericErrorEnvelope).error;
  return typeof err.code === "string" ? err.code : null;
}

function genericErrorMessage(body: unknown): string | null {
  const normalized = unwrapError(body);
  if (!normalized) return null;
  const err = (normalized as GenericErrorEnvelope).error;
  return typeof err.message === "string" ? err.message : null;
}

// ---------------------------------------------------------------------------
// Idempotency key
// ---------------------------------------------------------------------------

function newIdempotencyKey(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PortfolioStartDialog(
  props: PortfolioStartDialogProps,
): React.ReactElement {
  const { revision, open, onOpenChange, onSuccess } = props;
  const { getToken } = useAuth();
  const { scope } = useAccountScope();

  // Registry of registered broker accounts — the explicit target options.
  const accountsQ = useQuery({
    queryKey: ["broker-accounts"],
    queryFn: async () => listBrokerAccounts(await getToken()),
    enabled: open,
  });
  const accounts = useMemo<BrokerAccount[]>(
    () => accountsQ.data ?? [],
    [accountsQ.data],
  );

  // Form state — an EXPLICIT target account (replaces free-text account_id).
  const [selectedAccountId, setSelectedAccountId] = useState<string>("");
  const [confirmInput, setConfirmInput] = useState<string>("");
  const [formError, setFormError] = useState<string | null>(null);

  const selectedAccount = useMemo<BrokerAccount | null>(
    () => accounts.find((a) => a.id === selectedAccountId) ?? null,
    [accounts, selectedAccountId],
  );
  // paper_trading is DERIVED from the chosen account, never a separate toggle.
  const paperTrading = selectedAccount?.trading_mode === "paper";
  const isRealMoney = selectedAccount?.is_real_money === true;

  // Workflow state
  const [stage, setStage] = useState<Stage>("form");
  const [members, setMembers] = useState<LivePortfolioMemberFrozen[] | null>(
    null,
  );
  const [membersLoading, setMembersLoading] = useState<boolean>(false);
  const [submitting, setSubmitting] = useState<boolean>(false);

  // Error state for submit
  const [mismatches, setMismatches] = useState<BindingMismatchEntry[] | null>(
    null,
  );
  const [conflict, setConflict] = useState<{
    existingId: string;
    status?: string;
  } | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  // Persist the Idempotency-Key across deploy retries within the same dialog
  // session. Key resets when the dialog re-opens or when the identity-bearing
  // input (the chosen account) changes.
  const [idempotencyKey, setIdempotencyKey] = useState<string>(() =>
    newIdempotencyKey(),
  );

  // Pre-fill the target from the global account selector ONLY when scope is a
  // single concrete account that is registered. "all"/"unassigned" leave it
  // unset (operator must pick — US-002).
  useEffect(() => {
    if (!open) return;
    if (selectedAccountId) return; // don't override an explicit pick
    if (scope === "all" || scope === "unassigned") return;
    const match = accounts.find((a) => a.ib_account_id === scope);
    if (match) setSelectedAccountId(match.id);
  }, [open, scope, accounts, selectedAccountId]);

  // Reset when dialog closes
  useEffect(() => {
    if (!open) {
      setStage("form");
      setSelectedAccountId("");
      setConfirmInput("");
      setFormError(null);
      setMembers(null);
      setMembersLoading(false);
      setSubmitting(false);
      setMismatches(null);
      setConflict(null);
      setSubmitError(null);
      setIdempotencyKey(newIdempotencyKey());
    }
  }, [open]);

  // Rotate the idempotency key when the identity-bearing input (the chosen
  // account) changes — a different target is a genuinely new request.
  useEffect(() => {
    setIdempotencyKey(newIdempotencyKey());
  }, [selectedAccountId]);

  // ── Stage 1 → 2: validate target + load members ──────────────────────────
  const onPreview = useCallback(async (): Promise<void> => {
    setFormError(null);

    if (!selectedAccount) {
      setFormError("Pick a target account to deploy.");
      return;
    }

    setMembersLoading(true);
    try {
      const token = await getToken();
      const rows = await getRevisionMembers(revision.id, token);
      setMembers(rows);
      setStage("preview");
    } catch (err) {
      setFormError(describeApiError(err, "Failed to load revision members."));
    } finally {
      setMembersLoading(false);
    }
  }, [selectedAccount, revision.id, getToken]);

  // ── Submit (Stage 4) ──────────────────────────────────────────────────────
  const doSubmit = useCallback(async (): Promise<void> => {
    if (!selectedAccount) {
      setFormError("Pick a target account to deploy.");
      setStage("form");
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    setMismatches(null);
    setConflict(null);
    setStage("submitting");

    const realMoney = selectedAccount.is_real_money === true;
    const errorStage: Stage = realMoney ? "confirm" : "preview";

    try {
      const token = await getToken();
      const result = await startPortfolio(
        {
          portfolio_revision_id: revision.id,
          broker_account_id: selectedAccount.id,
          paper_trading: selectedAccount.trading_mode === "paper",
          confirm_account_id: realMoney ? confirmInput.trim() : undefined,
          selector_context_account_id: scope,
        },
        idempotencyKey,
        token,
      );
      onSuccess?.(result);
      onOpenChange(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        if (isBindingMismatch(err.body)) {
          const env = unwrapError(err.body) as BindingMismatchEnvelope;
          setMismatches(env.error.details.mismatches);
        } else if (isLiveDeployConflict(err.body)) {
          const env = unwrapError(err.body) as LiveDeployConflictEnvelope;
          setConflict({
            existingId: env.error.details.existing_deployment_id,
            status: env.error.details.status,
          });
        } else {
          // Decode the PR4 real-money / ambiguous-target codes; fall back to
          // the server's message. These should be unreachable from the UI
          // happy path (the dialog gates client-side + sends exactly one
          // target), but the server is the authority and its message must
          // render if it ever returns.
          const code = errorCode(err.body);
          const msg = genericErrorMessage(err.body);
          if (
            code === "REAL_MONEY_CONFIRM_REQUIRED" ||
            code === "REAL_MONEY_CONFIRM_MISMATCH" ||
            code === "AMBIGUOUS_DEPLOY_TARGET"
          ) {
            setSubmitError(msg ?? `Deployment rejected (${code}).`);
          } else {
            setSubmitError(msg ?? "Deployment was rejected (422).");
          }
        }
        setStage(errorStage);
        return;
      }
      setSubmitError(describeApiError(err, "Deploy failed."));
      setStage(errorStage);
    } finally {
      setSubmitting(false);
    }
  }, [
    selectedAccount,
    revision.id,
    getToken,
    confirmInput,
    scope,
    idempotencyKey,
    onSuccess,
    onOpenChange,
  ]);

  // ── Continue from Preview ──────────────────────────────────────────────────
  const onContinueFromPreview = useCallback((): void => {
    setMismatches(null);
    setConflict(null);
    setSubmitError(null);
    if (isRealMoney) {
      setStage("confirm");
    } else {
      void doSubmit();
    }
  }, [isRealMoney, doSubmit]);

  // Real-money confirm: the operator must type the exact ib_account_id.
  const confirmChallenge = selectedAccount?.ib_account_id ?? "";
  const confirmMatches = useMemo<boolean>(
    () =>
      confirmInput.trim() === confirmChallenge && confirmChallenge.length > 0,
    [confirmInput, confirmChallenge],
  );

  const noTargetPicked = !selectedAccount;

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Deploy portfolio revision</DialogTitle>
          <DialogDescription>
            Revision #{revision.revision_number} ·{" "}
            <span className="font-mono text-xs">
              {revision.composition_hash.slice(0, 12)}
            </span>
          </DialogDescription>
        </DialogHeader>

        {/* ─── Stage 1: Form (explicit target picker) ─────────────────── */}
        {stage === "form" && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="portfolio-start-target-account">
                Target account
              </Label>
              <Select
                value={selectedAccountId}
                onValueChange={setSelectedAccountId}
                disabled={accountsQ.isPending || accounts.length === 0}
              >
                <SelectTrigger
                  id="portfolio-start-target-account"
                  data-testid="portfolio-start-target-account"
                  aria-label="Target account"
                >
                  <SelectValue
                    placeholder={
                      accountsQ.isPending
                        ? "Loading accounts…"
                        : accountsQ.isError
                          ? "Couldn't load accounts"
                          : accounts.length === 0
                            ? "No registered accounts"
                            : "Pick a target account"
                    }
                  />
                </SelectTrigger>
                <SelectContent>
                  {accounts.map((a) => (
                    <SelectItem
                      key={a.id}
                      value={a.id}
                      data-testid={`portfolio-start-target-option-${a.id}`}
                      className={
                        a.is_real_money
                          ? "font-semibold text-destructive"
                          : undefined
                      }
                    >
                      {a.is_real_money
                        ? `⚠ REAL FUND — ${a.label ?? a.ib_account_id} (${a.ib_account_id}) — LIVE MONEY`
                        : `${a.label ?? a.ib_account_id} (${a.ib_account_id}) — ${a.trading_mode}`}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Deploys go to an EXPLICIT registered account. Paper vs live is
                taken from the account, not a toggle.
              </p>
            </div>

            {/* Codex code-review iter-3 P2: distinguish a FAILED /broker-accounts
                load from a genuinely empty list. On error, surface it + a retry
                instead of silently showing "No registered accounts" (which would
                block the operator when accounts actually exist). */}
            {accountsQ.isError && (
              <div
                role="alert"
                data-testid="portfolio-start-accounts-error"
                className="flex w-full items-center justify-between gap-3 rounded-md border border-destructive/30 bg-destructive/15 px-4 py-2 text-sm text-destructive"
              >
                <span>
                  Couldn&apos;t load broker accounts —{" "}
                  {describeApiError(accountsQ.error, "try again")}.
                </span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => void accountsQ.refetch()}
                >
                  Retry
                </Button>
              </div>
            )}

            {!accountsQ.isError && noTargetPicked && (
              <div
                role="status"
                data-testid="portfolio-start-no-target-notice"
                className="w-full rounded-md border border-amber-500/40 bg-amber-500/10 px-4 py-2 text-sm text-amber-300"
              >
                Pick a target account to deploy.
              </div>
            )}

            {selectedAccount && isRealMoney && (
              <div
                role="alert"
                className="w-full rounded-md border border-destructive/30 bg-destructive/15 px-4 py-3 text-sm text-destructive"
              >
                <strong className="font-semibold">⚠ REAL FUND:</strong> this is
                the production fund account (
                <span className="font-mono">
                  {selectedAccount.ib_account_id}
                </span>
                ). You will be asked to type the account id to confirm before
                deploy.
              </div>
            )}

            {selectedAccount && !isRealMoney && !paperTrading && (
              <div
                role="alert"
                className="w-full rounded-md border border-destructive/30 bg-destructive/15 px-4 py-3 text-sm text-destructive"
              >
                <strong className="font-semibold">⚠ LIVE:</strong> orders
                submitted by this deployment execute against the live test
                account{" "}
                <span className="font-mono">
                  {selectedAccount.ib_account_id}
                </span>
                .
              </div>
            )}

            {formError && (
              <div
                role="alert"
                className="w-full rounded-md border border-destructive/30 bg-destructive/15 px-4 py-2 text-sm text-destructive"
              >
                {formError}
              </div>
            )}

            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={membersLoading}
              >
                Cancel
              </Button>
              <Button
                data-testid="portfolio-start-preview-button"
                onClick={() => void onPreview()}
                disabled={membersLoading || noTargetPicked}
              >
                {membersLoading ? "Loading…" : "Preview"}
              </Button>
            </DialogFooter>
          </div>
        )}

        {/* ─── Stage 2: Preview ──────────────────────────────────────── */}
        {stage === "preview" && (
          <div className="flex flex-col gap-4">
            <div className="max-h-[40vh] overflow-y-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Strategy</TableHead>
                    <TableHead>Instruments</TableHead>
                    <TableHead className="text-right">Weight</TableHead>
                    <TableHead>Config</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(members ?? []).map((m) => (
                    <TableRow key={m.id}>
                      <TableCell className="font-mono text-xs">
                        {m.strategy_id.slice(0, 8)}…
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {m.instruments.join(", ")}
                      </TableCell>
                      <TableCell className="text-right font-mono text-xs">
                        {m.weight}
                      </TableCell>
                      <TableCell className="max-w-[18rem] truncate font-mono text-xs text-muted-foreground">
                        {JSON.stringify(m.config)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>

            <p className="text-xs text-muted-foreground">
              Target:{" "}
              <span className="font-mono">
                {selectedAccount?.ib_account_id}
              </span>{" "}
              ({paperTrading ? "paper" : "live"}). Binding contract: matching
              config + instruments will be verified server-side. Mismatches will
              be returned as 422 with field-level diff.
            </p>

            {renderErrors({ mismatches, conflict, submitError })}

            <DialogFooter>
              <Button variant="outline" onClick={() => setStage("form")}>
                Back
              </Button>
              <Button
                data-testid="portfolio-start-deploy-button"
                onClick={onContinueFromPreview}
                disabled={submitting}
              >
                {isRealMoney
                  ? "Continue to Deploy"
                  : paperTrading
                    ? "Deploy (paper)"
                    : "Deploy (live)"}
              </Button>
            </DialogFooter>
          </div>
        )}

        {/* ─── Stage 3: Confirm (real-money / fund only) ────────────── */}
        {stage === "confirm" && (
          <div className="flex flex-col gap-4">
            <div
              role="alert"
              className="w-full rounded-md border border-destructive/30 bg-destructive/15 px-4 py-3 text-sm text-destructive"
            >
              <strong className="font-semibold">
                ⚠ REAL FUND — {confirmChallenge} — LIVE MONEY.
              </strong>{" "}
              Type the account id{" "}
              <span className="font-mono">{confirmChallenge}</span> exactly to
              confirm.
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="portfolio-start-confirm-input">
                Confirm account id
              </Label>
              <Input
                id="portfolio-start-confirm-input"
                data-testid="portfolio-start-confirm-input"
                value={confirmInput}
                onChange={(e) => setConfirmInput(e.target.value)}
                placeholder={confirmChallenge}
                autoComplete="off"
              />
            </div>

            {renderErrors({ mismatches, conflict, submitError })}

            <DialogFooter>
              <Button variant="outline" onClick={() => setStage("preview")}>
                Back
              </Button>
              <Button
                variant="destructive"
                data-testid="portfolio-start-deploy-button"
                disabled={!confirmMatches || submitting}
                onClick={() => void doSubmit()}
              >
                {submitting ? "Deploying…" : "Deploy (REAL MONEY)"}
              </Button>
            </DialogFooter>
          </div>
        )}

        {/* ─── Stage 4: Submitting spinner ──────────────────────────── */}
        {stage === "submitting" && (
          <div className="flex flex-col items-center gap-3 py-8 text-sm text-muted-foreground">
            <div
              aria-hidden
              className="size-6 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-foreground motion-reduce:animate-none"
            />
            Deploying…
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Inline error renderer (shared between Preview + Confirm stages)
// ---------------------------------------------------------------------------

function renderErrors(args: {
  mismatches: BindingMismatchEntry[] | null;
  conflict: { existingId: string; status?: string } | null;
  submitError: string | null;
}): React.ReactElement | null {
  const { mismatches, conflict, submitError } = args;
  if (!mismatches && !conflict && !submitError) return null;

  return (
    <div className="flex flex-col gap-3">
      {mismatches && mismatches.length > 0 && (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3">
          <p className="mb-2 text-sm font-semibold text-destructive">
            Binding mismatch — frozen revision differs from current strategy
            registry.
          </p>
          <div
            data-testid="portfolio-start-mismatches-table"
            className="max-h-[24vh] overflow-auto rounded border bg-background"
          >
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Field</TableHead>
                  <TableHead>Member value (frozen)</TableHead>
                  <TableHead>Candidate value (current)</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {mismatches.map((m, i) => (
                  <TableRow key={`${m.field}-${i}`}>
                    <TableCell className="font-mono text-xs">
                      {m.field}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {JSON.stringify(m.member_value)}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {JSON.stringify(m.candidate_value)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}

      {conflict && (
        <div className="flex flex-col gap-2 rounded-md border border-destructive/30 bg-destructive/10 p-3">
          <p className="text-sm font-semibold text-destructive">
            Live deploy conflict
          </p>
          <p className="text-xs text-destructive">
            An existing deployment row already binds this (revision_id,
            account_id) under a different identity:
            <span className="ml-1 font-mono">{conflict.existingId}</span>
            {conflict.status ? (
              <>
                {" "}
                (status: <span className="font-mono">{conflict.status}</span>)
              </>
            ) : null}
            .
          </p>
          <p className="text-xs text-destructive">
            <strong>Remediation:</strong> re-submit with the same{" "}
            <span className="font-mono">ib_login_key</span> +{" "}
            <span className="font-mono">paper_trading</span> as the existing
            row, OR archive the existing deployment row (manual operator step —
            there is no public archive endpoint yet).
          </p>
        </div>
      )}

      {submitError && (
        <div
          role="alert"
          className="w-full rounded-md border border-destructive/30 bg-destructive/15 px-4 py-2 text-sm text-destructive"
        >
          {submitError}
        </div>
      )}
    </div>
  );
}
