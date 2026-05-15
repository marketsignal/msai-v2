"use client";

/**
 * PortfolioStartDialog — 4-stage deploy flow for a frozen
 * LivePortfolioRevision.
 *
 *   Stage 1: Form (account_id, ib_login_key, paper_trading toggle)
 *   Stage 2: Preview (GET revision members)
 *   Stage 3: Real-money confirm (paper skips this stage)
 *   Stage 4: Submit (POST /api/v1/live/start-portfolio with Idempotency-Key)
 *
 * 422 envelopes are decoded inline:
 *   - BINDING_MISMATCH    → mismatches table (field/member_value/candidate_value)
 *   - LIVE_DEPLOY_CONFLICT → remediation callout (no retry CTA — Codex iter-2 P2
 *                            found stop+retry hits the same 422 since the
 *                            backend collision check runs against the persistent
 *                            row regardless of active status)
 *   - other 422 codes      → red callout with body.error.message
 *
 * Accepts HTTP 200 OR 201 as success (warm-restart vs cold). `startPortfolio`
 * throws ApiError on non-2xx, so both reach the success path naturally.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useAuth } from "@/lib/auth";
import { ApiError, getRevisionMembers, startPortfolio } from "@/lib/api";
import type {
  LivePortfolioMemberFrozen,
  LivePortfolioRevision,
  PortfolioStartResponse,
} from "@/lib/api";

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

  // Form state
  const [accountId, setAccountId] = useState<string>("");
  const [ibLoginKey, setIbLoginKey] = useState<string>("");
  const [paperTrading, setPaperTrading] = useState<boolean>(true);
  const [confirmInput, setConfirmInput] = useState<string>("");
  const [formError, setFormError] = useState<string | null>(null);

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
  // Codex iter-8 P2: persist the Idempotency-Key across deploy retries
  // within the same dialog session. If the POST reaches the backend but
  // the browser loses the response, a retry with a fresh key would
  // bypass the Redis reservation and could publish a second START
  // before active-process de-dupe observes the first. Key resets when
  // the dialog re-opens or when identity-bearing inputs change.
  const [idempotencyKey, setIdempotencyKey] = useState<string>(() =>
    newIdempotencyKey(),
  );

  // Reset when dialog closes
  useEffect(() => {
    if (!open) {
      setStage("form");
      setAccountId("");
      setIbLoginKey("");
      setPaperTrading(true);
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

  // Codex iter-8 P2: rotate the key when the *identity-bearing* fields
  // change (a different account or login is genuinely a new request,
  // not a retry). Paper toggle is part of identity_signature too.
  useEffect(() => {
    setIdempotencyKey(newIdempotencyKey());
  }, [accountId, ibLoginKey, paperTrading]);

  // ── Stage 1 → 2: validate + load members ────────────────────────────────
  const onPreview = useCallback(async (): Promise<void> => {
    setFormError(null);

    if (!accountId.trim()) {
      setFormError("Account ID is required.");
      return;
    }
    if (!ibLoginKey.trim()) {
      setFormError("IB login key is required.");
      return;
    }
    // Codex iter-4 P2: match backend IB_PAPER_PREFIXES = ("DU", "DF").
    // DU = personal paper; DF/DFP = FA sub-account paper. Live accounts
    // start with U (not DU/DF). Trim before checking — operators paste
    // account IDs with trailing whitespace from the IB portal.
    const trimmedAccount = accountId.trim();
    const isPaperPrefix =
      trimmedAccount.startsWith("DU") || trimmedAccount.startsWith("DF");
    if (paperTrading && !isPaperPrefix) {
      setFormError("Paper accounts must start with 'DU' or 'DF'.");
      return;
    }
    if (!paperTrading && (isPaperPrefix || !trimmedAccount.startsWith("U"))) {
      setFormError("Live accounts must start with 'U' (not DU/DF).");
      return;
    }

    setMembersLoading(true);
    try {
      const token = await getToken();
      const rows = await getRevisionMembers(revision.id, token);
      setMembers(rows);
      setStage("preview");
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `Failed to load revision members: ${err.status}`
          : err instanceof Error
            ? err.message
            : "Failed to load revision members.";
      setFormError(msg);
    } finally {
      setMembersLoading(false);
    }
  }, [accountId, ibLoginKey, paperTrading, revision.id, getToken]);

  // ── Submit (Stage 4) ────────────────────────────────────────────────────
  const doSubmit = useCallback(async (): Promise<void> => {
    setSubmitting(true);
    setSubmitError(null);
    setMismatches(null);
    setConflict(null);
    setStage("submitting");

    try {
      const token = await getToken();
      // Codex iter-5 P2: submit trimmed values. onPreview validates the
      // trimmed account_id but the raw input could still carry leading/
      // trailing whitespace from a paste. The backend uses these strings
      // verbatim for identity_signature + IB login routing, so " DU123"
      // vs "DU123" can become distinct deployment rows targeting the
      // same broker account, and " mslvp000" fails gateway-route lookup.
      const result = await startPortfolio(
        {
          portfolio_revision_id: revision.id,
          account_id: accountId.trim(),
          paper_trading: paperTrading,
          ib_login_key: ibLoginKey.trim(),
        },
        // Codex iter-8 P2: reuse the dialog's sticky key across retries.
        idempotencyKey,
        token,
      );
      onSuccess?.(result);
      onOpenChange(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        const errorStage: Stage = paperTrading ? "preview" : "confirm";
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
          const msg = genericErrorMessage(err.body);
          setSubmitError(msg ?? "Deployment was rejected (422).");
        }
        setStage(errorStage);
        return;
      }
      const msg =
        err instanceof ApiError
          ? `Deploy failed: ${err.status}`
          : err instanceof Error
            ? err.message
            : "Deploy failed.";
      setSubmitError(msg);
      setStage(paperTrading ? "preview" : "confirm");
    } finally {
      setSubmitting(false);
    }
  }, [
    accountId,
    ibLoginKey,
    paperTrading,
    revision.id,
    getToken,
    onSuccess,
    onOpenChange,
    idempotencyKey,
  ]);

  // ── Continue from Preview ───────────────────────────────────────────────
  const onContinueFromPreview = useCallback((): void => {
    setMismatches(null);
    setConflict(null);
    setSubmitError(null);
    if (paperTrading) {
      void doSubmit();
    } else {
      setStage("confirm");
    }
  }, [paperTrading, doSubmit]);

  // Codex code-review iter-2 P2: removed the "stop existing + retry"
  // resolver for LIVE_DEPLOY_CONFLICT. The backend check raises against
  // the persistent deployment row regardless of active status, so /stop
  // alone wouldn't clear the conflict — it would just loop. The UI now
  // surfaces the real remediation in the conflict callout instead.

  // PR #67 review P2: the preview path validates + the submit path
  // sends the TRIMMED account_id (Codex iter-9 fix), so the confirm
  // challenge must use the trimmed value on both sides too. Otherwise
  // operator who pasted " DU... " (leading/trailing whitespace) sees
  // a Deploy button that never enables — they'd have to type the same
  // invisible whitespace to satisfy the raw equality check, even though
  // the request will send the trimmed value regardless.
  const trimmedAccountId = useMemo(() => accountId.trim(), [accountId]);
  const confirmMatches = useMemo<boolean>(
    () =>
      confirmInput.trim() === trimmedAccountId && trimmedAccountId.length > 0,
    [confirmInput, trimmedAccountId],
  );

  // ── Render ──────────────────────────────────────────────────────────────
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

        {/* ─── Stage 1: Form ─────────────────────────────────────────── */}
        {stage === "form" && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="portfolio-start-account-id">Account ID</Label>
              <Input
                id="portfolio-start-account-id"
                data-testid="portfolio-start-account-id"
                value={accountId}
                onChange={(e) => setAccountId(e.target.value)}
                placeholder={paperTrading ? "DU1234567" : "U1234567"}
                autoComplete="off"
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="portfolio-start-ib-login-key">IB login key</Label>
              <Input
                id="portfolio-start-ib-login-key"
                data-testid="portfolio-start-ib-login-key"
                value={ibLoginKey}
                onChange={(e) => setIbLoginKey(e.target.value)}
                placeholder="ib-paper-1"
                autoComplete="off"
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label
                htmlFor="portfolio-start-paper-toggle"
                className="cursor-pointer"
              >
                <input
                  id="portfolio-start-paper-toggle"
                  data-testid="portfolio-start-paper-toggle"
                  type="checkbox"
                  checked={paperTrading}
                  onChange={(e) => setPaperTrading(e.target.checked)}
                  className="size-4 cursor-pointer rounded border-input accent-primary focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                />
                <span>Paper trading (recommended)</span>
              </Label>
            </div>

            {!paperTrading && (
              <div
                role="alert"
                className="w-full rounded-md border border-destructive/30 bg-destructive/15 px-4 py-3 text-sm text-destructive"
              >
                <strong className="font-semibold">⚠ REAL MONEY:</strong> orders
                submitted by this deployment will execute against your live IB
                account. Verify account_id matches your intent.
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
                disabled={membersLoading}
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
              Binding contract: matching config + instruments will be verified
              server-side. Mismatches will be returned as 422 with field-level
              diff.
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
                {paperTrading ? "Deploy (paper)" : "Continue to Deploy"}
              </Button>
            </DialogFooter>
          </div>
        )}

        {/* ─── Stage 3: Confirm (real-money only) ───────────────────── */}
        {stage === "confirm" && (
          <div className="flex flex-col gap-4">
            <div
              role="alert"
              className="w-full rounded-md border border-destructive/30 bg-destructive/15 px-4 py-3 text-sm text-destructive"
            >
              <strong className="font-semibold">⚠ REAL MONEY DEPLOY.</strong>{" "}
              Type the account ID{" "}
              <span className="font-mono">{trimmedAccountId}</span> exactly to
              confirm.
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="portfolio-start-confirm-input">
                Confirm account ID
              </Label>
              <Input
                id="portfolio-start-confirm-input"
                data-testid="portfolio-start-confirm-input"
                value={confirmInput}
                onChange={(e) => setConfirmInput(e.target.value)}
                placeholder={trimmedAccountId}
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
            {/* Codex code-review iter-2 P2: LIVE_DEPLOY_CONFLICT is
               raised against the persistent live_deployments row
               regardless of active status (api/live.py "LIVE_DEPLOY_CONFLICT"
               check). A /stop call only marks the row as `stopped`; it
               doesn't archive it. Re-deploying with a different identity
               would hit the same 422. The actual remediation is either:
               (a) re-submit with the SAME identity (ib_login_key,
               paper_trading) as the existing row, or (b) operator deletes
               the deployment row via DB/admin path. We surface that here
               instead of offering a "Stop existing + retry" CTA that
               would lead the operator into a retry loop. */}
            <strong>Remediation:</strong> re-submit with the same{" "}
            <span className="font-mono">ib_login_key</span> +{" "}
            <span className="font-mono">paper_trading</span> as the existing
            row, OR archive the existing deployment row (manual operator step —
            there is no public archive endpoint yet).
          </p>
          {/* Intentionally NO retry CTA — see comment above. */}
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
