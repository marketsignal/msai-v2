"use client";

/**
 * BrokerAccountWizard — multi-step "Add account" dialog for the IB broker
 * fleet, mirroring the staged form → review → submit machine in
 * `components/live/portfolio-start-dialog.tsx`.
 *
 *   Step 1 (identity):    ib_account_id, ib_login_key, trading_mode
 *                         (paper|live), optional gateway_slot.
 *   Step 2 (credentials): tws_userid + tws_password as MASKED inputs.
 *   Step 3 (review):      shows everything EXCEPT the password, then Create
 *                         → POST /api/v1/broker-accounts (createBrokerAccount).
 *
 * Trust-First treatment:
 *   - The password input is `type="password"`; it is NEVER echoed back on the
 *     review step and never pre-filled.
 *   - This wizard is ADD-ONLY. Editing an existing account (label /
 *     trading_mode) and rotating credentials are handled by
 *     `broker-account-detail.tsx` (T15), which never round-trips the secret —
 *     so the wizard never needs an edit mode that would risk echoing it.
 *
 * On success: a toast naming the account, the ["broker-accounts"] query is
 * invalidated (so the list refreshes), and the dialog closes. Inline errors
 * (409 duplicate / 409 no free slot / 422 validation) are decoded via
 * `describeApiError` and shown in a red callout on the originating step.
 */

import { useCallback, useEffect, useState } from "react";

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
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  createBrokerAccount,
  type BrokerAccount,
} from "@/lib/api/broker-accounts";
import { describeApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface BrokerAccountWizardProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Fired after a successful create, with the new (metadata-only) account. */
  onCreated?: (account: BrokerAccount) => void;
}

type Step = "identity" | "credentials" | "review";
type TradingMode = "paper" | "live";
/** For live accounts the operator chooses whether it is a Test account
 * (LVP/HVP — live IB, limited capital, NOT the fund) or the production Fund
 * (real money — identity-echo gated on deploy). Paper accounts are always the
 * "paper" class server-side. */
type LiveClass = "test" | "real";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function BrokerAccountWizard({
  open,
  onOpenChange,
  onCreated,
}: BrokerAccountWizardProps): React.ReactElement {
  const { getToken } = useAuth();
  const qc = useQueryClient();

  // Form state
  const [step, setStep] = useState<Step>("identity");
  const [ibAccountId, setIbAccountId] = useState("");
  const [ibLoginKey, setIbLoginKey] = useState("");
  const [tradingMode, setTradingMode] = useState<TradingMode>("paper");
  // For live accounts only: Test (default) vs Fund (real money). Ignored for
  // paper (server derives "paper"). Default Test so the fund is never the
  // accidental default.
  const [liveClass, setLiveClass] = useState<LiveClass>("test");
  const [gatewaySlot, setGatewaySlot] = useState("");
  const [twsUserid, setTwsUserid] = useState("");
  const [twsPassword, setTwsPassword] = useState("");

  // Per-step inline error
  const [stepError, setStepError] = useState<string | null>(null);

  // Reset everything when the dialog closes — Trust-First: the typed password
  // must not survive a close/reopen.
  useEffect(() => {
    if (!open) {
      setStep("identity");
      setIbAccountId("");
      setIbLoginKey("");
      setTradingMode("paper");
      setLiveClass("test");
      setGatewaySlot("");
      setTwsUserid("");
      setTwsPassword("");
      setStepError(null);
    }
  }, [open]);

  const mutation = useMutation({
    mutationFn: async (): Promise<BrokerAccount> => {
      const token = await getToken();
      const trimmedSlot = gatewaySlot.trim();
      return createBrokerAccount(
        {
          ib_account_id: ibAccountId.trim(),
          ib_login_key: ibLoginKey.trim(),
          trading_mode: tradingMode,
          // PR4: paper → server derives "paper"; live → operator's Test/Fund
          // choice ("test" | "real"). The fund is registered as "real".
          account_class: tradingMode === "live" ? liveClass : undefined,
          gateway_slot: trimmedSlot === "" ? null : trimmedSlot,
          tws_userid: twsUserid.trim(),
          tws_password: twsPassword,
        },
        token,
      );
    },
    onSuccess: (created) => {
      void qc.invalidateQueries({ queryKey: ["broker-accounts"] });
      toast.success("Broker account created", {
        description: `${created.ib_account_id} — gateway slot ${created.gateway_slot} (${created.trading_mode}).`,
      });
      onCreated?.(created);
      onOpenChange(false);
    },
    onError: (err) => {
      // 409 duplicate / 409 no free slot / 422 validation all surface here as
      // a human-readable string on the review step.
      setStepError(
        describeApiError(err, "Failed to create the broker account."),
      );
    },
  });

  // ── Step 1 → 2: validate identity ────────────────────────────────────────
  const onNextFromIdentity = useCallback((): void => {
    setStepError(null);
    const trimmedAccount = ibAccountId.trim();
    if (!trimmedAccount) {
      setStepError("IB account id is required.");
      return;
    }
    if (!ibLoginKey.trim()) {
      setStepError("IB login key is required.");
      return;
    }
    // Mirror the backend IB_PAPER_PREFIXES = ("DU", "DF") convention used in
    // the portfolio-start dialog. Paper accounts start with DU/DF; live
    // accounts start with U (not DU/DF).
    const isPaperPrefix =
      trimmedAccount.startsWith("DU") || trimmedAccount.startsWith("DF");
    if (tradingMode === "paper" && !isPaperPrefix) {
      setStepError("Paper accounts must start with 'DU' or 'DF'.");
      return;
    }
    if (
      tradingMode === "live" &&
      (isPaperPrefix || !trimmedAccount.startsWith("U"))
    ) {
      setStepError("Live accounts must start with 'U' (not DU/DF).");
      return;
    }
    setStep("credentials");
  }, [ibAccountId, ibLoginKey, tradingMode]);

  // ── Step 2 → 3: validate credentials ─────────────────────────────────────
  const onNextFromCredentials = useCallback((): void => {
    setStepError(null);
    if (!twsUserid.trim()) {
      setStepError("TWS user id is required.");
      return;
    }
    if (twsPassword === "") {
      setStepError("TWS password is required.");
      return;
    }
    setStep("review");
  }, [twsUserid, twsPassword]);

  const trimmedSlot = gatewaySlot.trim();

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="sm:max-w-lg"
        data-testid="broker-account-wizard"
      >
        <DialogHeader>
          <DialogTitle>Add broker account</DialogTitle>
          <DialogDescription>
            Register an Interactive Brokers account in the fleet. Credentials
            are written straight to the secrets backend and never shown again.
          </DialogDescription>
        </DialogHeader>

        {/* ─── Step 1: Identity ──────────────────────────────────────── */}
        {step === "identity" && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="broker-account-ib-account-id">
                IB account id
              </Label>
              <Input
                id="broker-account-ib-account-id"
                data-testid="broker-account-ib-account-id"
                value={ibAccountId}
                onChange={(e) => setIbAccountId(e.target.value)}
                placeholder={tradingMode === "paper" ? "DU1234567" : "U1234567"}
                autoComplete="off"
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="broker-account-ib-login-key">IB login key</Label>
              <Input
                id="broker-account-ib-login-key"
                data-testid="broker-account-ib-login-key"
                value={ibLoginKey}
                onChange={(e) => setIbLoginKey(e.target.value)}
                placeholder="ib-paper-1"
                autoComplete="off"
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="broker-account-trading-mode">Trading mode</Label>
              <Select
                value={tradingMode}
                onValueChange={(v) => setTradingMode(v as TradingMode)}
              >
                <SelectTrigger
                  id="broker-account-trading-mode"
                  data-testid="broker-account-trading-mode"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="paper">Paper</SelectItem>
                  <SelectItem value="live">Live</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {tradingMode === "live" && (
              <div className="flex flex-col gap-2">
                <Label htmlFor="broker-account-account-class">
                  Account class
                </Label>
                <Select
                  value={liveClass}
                  onValueChange={(v) => setLiveClass(v as LiveClass)}
                >
                  <SelectTrigger
                    id="broker-account-account-class"
                    data-testid="broker-account-account-class"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="test">
                      Test account (LVP/HVP — live, not the fund)
                    </SelectItem>
                    <SelectItem value="real">
                      Fund (REAL MONEY — the production fund)
                    </SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  Test = a live IB account with limited capital for drills. Fund
                  = the real production fund; deploys to it require typing the
                  account id to confirm.
                </p>
              </div>
            )}

            <div className="flex flex-col gap-2">
              <Label htmlFor="broker-account-gateway-slot">
                Gateway slot{" "}
                <span className="text-muted-foreground">(optional)</span>
              </Label>
              <Input
                id="broker-account-gateway-slot"
                data-testid="broker-account-gateway-slot"
                value={gatewaySlot}
                onChange={(e) => setGatewaySlot(e.target.value)}
                placeholder="Auto-allocate a free slot"
                autoComplete="off"
              />
              <p className="text-xs text-muted-foreground">
                Leave blank to auto-allocate the next free gateway slot.
              </p>
            </div>

            {tradingMode === "live" && liveClass === "real" && (
              <div
                role="alert"
                className="w-full rounded-md border border-destructive/30 bg-destructive/15 px-4 py-3 text-sm text-destructive"
              >
                <strong className="font-semibold">⚠ REAL FUND:</strong> this
                registers the production fund. Orders routed through it execute
                against real money, and deploys require typing the account id to
                confirm.
              </div>
            )}

            {stepError && <ErrorCallout message={stepError} />}

            <DialogFooter>
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button
                data-testid="broker-account-wizard-next"
                onClick={onNextFromIdentity}
              >
                Next
              </Button>
            </DialogFooter>
          </div>
        )}

        {/* ─── Step 2: Credentials ───────────────────────────────────── */}
        {step === "credentials" && (
          <div className="flex flex-col gap-4">
            <p className="text-xs text-muted-foreground">
              These TWS credentials are written to the secrets backend on create
              and are never echoed back — not even on the review step.
            </p>

            <div className="flex flex-col gap-2">
              <Label htmlFor="broker-account-tws-userid">TWS user id</Label>
              <Input
                id="broker-account-tws-userid"
                data-testid="broker-account-tws-userid"
                value={twsUserid}
                onChange={(e) => setTwsUserid(e.target.value)}
                autoComplete="off"
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="broker-account-tws-password">TWS password</Label>
              <Input
                id="broker-account-tws-password"
                data-testid="broker-account-tws-password"
                type="password"
                value={twsPassword}
                onChange={(e) => setTwsPassword(e.target.value)}
                autoComplete="new-password"
              />
            </div>

            {stepError && <ErrorCallout message={stepError} />}

            <DialogFooter>
              <Button
                variant="outline"
                data-testid="broker-account-wizard-back"
                onClick={() => {
                  setStepError(null);
                  setStep("identity");
                }}
              >
                Back
              </Button>
              <Button
                data-testid="broker-account-wizard-next"
                onClick={onNextFromCredentials}
              >
                Next
              </Button>
            </DialogFooter>
          </div>
        )}

        {/* ─── Step 3: Review ────────────────────────────────────────── */}
        {step === "review" && (
          <div className="flex flex-col gap-4">
            <dl
              className="grid grid-cols-1 gap-3 rounded-md border border-border/50 bg-card/40 p-4 sm:grid-cols-2"
              data-testid="broker-account-wizard-review"
            >
              <ReviewRow
                label="IB account id"
                value={ibAccountId.trim()}
                mono
              />
              <ReviewRow label="IB login key" value={ibLoginKey.trim()} mono />
              <ReviewRow label="Trading mode" value={tradingMode} />
              <ReviewRow
                label="Account class"
                value={
                  tradingMode === "paper"
                    ? "paper"
                    : liveClass === "real"
                      ? "REAL FUND"
                      : "test"
                }
              />
              <ReviewRow
                label="Gateway slot"
                value={trimmedSlot === "" ? "Auto-allocate" : trimmedSlot}
                mono={trimmedSlot !== ""}
              />
              <ReviewRow label="TWS user id" value={twsUserid.trim()} mono />
              {/* Trust-First: the password is intentionally NOT shown. */}
              <ReviewRow label="TWS password" value="•••••••• (hidden)" />
            </dl>

            <p className="text-xs text-muted-foreground">
              The password is never displayed. On create it is written to the
              secrets backend; the response carries only a reference and audit
              metadata.
            </p>

            {stepError && <ErrorCallout message={stepError} />}

            <DialogFooter>
              <Button
                variant="outline"
                data-testid="broker-account-wizard-back"
                disabled={mutation.isPending}
                onClick={() => {
                  setStepError(null);
                  setStep("credentials");
                }}
              >
                Back
              </Button>
              <Button
                data-testid="broker-account-wizard-create"
                disabled={mutation.isPending}
                onClick={() => mutation.mutate()}
              >
                {mutation.isPending ? "Creating…" : "Create"}
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function ErrorCallout({ message }: { message: string }): React.ReactElement {
  return (
    <div
      role="alert"
      data-testid="broker-account-wizard-error"
      className="w-full rounded-md border border-destructive/30 bg-destructive/15 px-4 py-2 text-sm text-destructive"
    >
      {message}
    </div>
  );
}

function ReviewRow({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}): React.ReactElement {
  return (
    <div className="space-y-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd
        className={`text-sm break-all ${mono ? "font-mono" : ""}`}
        title={value}
      >
        {value}
      </dd>
    </div>
  );
}
