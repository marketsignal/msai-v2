"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Archive, KeyRound, Pencil, ShieldCheck } from "lucide-react";

import {
  archiveBrokerAccount,
  rotateBrokerAccountCredentials,
  updateBrokerAccount,
  type BrokerAccount,
  type BrokerAccountUpdate,
} from "@/lib/api/broker-accounts";
import { describeApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatTimestamp } from "@/lib/format";

interface BrokerAccountDetailProps {
  /** The selected account, or null when the sheet is closed. */
  account: BrokerAccount | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * Called with the refreshed account after a successful in-drawer mutation
   * (e.g. credential rotation) so the parent can update the selected account
   * and the open drawer reflects the new metadata without closing.
   */
  onUpdated?: (account: BrokerAccount) => void;
}

function formatMaybeTimestamp(value: string | null): string {
  if (!value) return "—";
  return formatTimestamp(value);
}

/**
 * Broker-account detail drawer.
 *
 * Trust-First treatment: shows credential METADATA only (secret reference,
 * version, who/when updated, last-accessed) — there is NEVER a password
 * field here. Two actions: rotate the stored credentials (a focused dialog
 * with masked inputs) and archive the account (a Trust-First confirmation
 * dialog that spells out the consequences before the destructive action).
 */
export function BrokerAccountDetail({
  account,
  open,
  onOpenChange,
  onUpdated,
}: BrokerAccountDetailProps): React.ReactElement {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-full sm:max-w-xl"
        data-testid="broker-account-detail"
      >
        {account ? (
          <DetailBody
            account={account}
            onArchived={() => onOpenChange(false)}
            onUpdated={onUpdated}
          />
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

function DetailBody({
  account,
  onArchived,
  onUpdated,
}: {
  account: BrokerAccount;
  onArchived: () => void;
  onUpdated?: (account: BrokerAccount) => void;
}): React.ReactElement {
  const isArchived = account.status.toLowerCase() === "archived";

  return (
    <>
      <SheetHeader>
        <SheetTitle className="flex items-center gap-2">
          <span className="font-mono">{account.ib_account_id}</span>
          <Badge variant="secondary" className="bg-muted text-muted-foreground">
            {account.status}
          </Badge>
        </SheetTitle>
        <SheetDescription>
          Gateway slot <code className="font-mono">{account.gateway_slot}</code>{" "}
          · {account.trading_mode} ·{" "}
          <span data-testid="broker-account-detail-class">
            {account.is_real_money
              ? "REAL FUND"
              : (account.account_class ?? "—")}
          </span>{" "}
          · {account.label ?? <span className="italic">no label</span>}
        </SheetDescription>
      </SheetHeader>

      <div className="space-y-6 px-4 pb-4 pt-2">
        <section className="space-y-3">
          <div className="flex items-center gap-2">
            <ShieldCheck
              className="size-4 text-muted-foreground"
              aria-hidden="true"
            />
            <h3 className="text-sm font-semibold">Credential metadata</h3>
          </div>
          <p className="text-xs text-muted-foreground">
            The TWS credentials live in the secrets backend and are never
            returned to this UI. Only the reference and audit trail are shown.
          </p>
          <dl
            className="grid grid-cols-1 gap-3 rounded-md border border-border/50 bg-card/40 p-4 sm:grid-cols-2"
            data-testid="broker-account-credential-metadata"
          >
            <MetaRow label="Backend" value={account.credentials_backend} mono />
            <MetaRow
              label="Secret reference"
              value={account.credentials_secret_ref}
              mono
            />
            <MetaRow
              label="Secret version"
              value={account.credentials_secret_version ?? "—"}
              mono
            />
            <MetaRow
              label="Updated"
              value={formatMaybeTimestamp(account.credentials_updated_at)}
            />
            <MetaRow
              label="Updated by"
              value={account.credentials_updated_by ?? "—"}
            />
            <MetaRow
              label="Last accessed"
              value={formatMaybeTimestamp(account.credentials_last_accessed)}
            />
          </dl>
        </section>

        <Separator className="bg-border/50" />

        <section className="space-y-3">
          <h3 className="text-sm font-semibold">Actions</h3>
          <div className="flex flex-wrap gap-2">
            <EditAccountDialog
              account={account}
              disabled={isArchived}
              onUpdated={onUpdated}
            />
            <RotateCredentialsDialog
              account={account}
              disabled={isArchived}
              onUpdated={onUpdated}
            />
            <ArchiveDialog
              account={account}
              disabled={isArchived}
              onArchived={onArchived}
            />
          </div>
          {isArchived ? (
            <p className="text-xs text-muted-foreground">
              This account is archived — its gateway slot is freed and it can no
              longer trade.
              {account.credentials_backend === "legacy_env"
                ? " Its environment-managed credentials are unchanged."
                : " The stored secret has been deleted."}
            </p>
          ) : null}
        </section>
      </div>
    </>
  );
}

function MetaRow({
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

type TradingMode = "paper" | "live";

/**
 * Edit the mutable fields of a broker account (label + trading mode).
 *
 * Only fields the operator actually changed are sent in the PATCH body: an
 * emptied label is sent as an explicit `null` (clearing the stored label),
 * while an untouched field is omitted entirely so the backend leaves it as-is
 * (the API distinguishes the two via `model_fields_set`). On success the
 * refreshed account is lifted to the parent's selected state and the list cache
 * is patched — same pattern as the rotate dialog — then revalidated.
 *
 * Disabled when the account is archived (archived is terminal — no edits).
 */
function EditAccountDialog({
  account,
  disabled,
  onUpdated,
}: {
  account: BrokerAccount;
  disabled: boolean;
  onUpdated?: (account: BrokerAccount) => void;
}): React.ReactElement {
  const { getToken } = useAuth();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState(account.label ?? "");
  const [tradingMode, setTradingMode] = useState<TradingMode>(
    account.trading_mode === "live" ? "live" : "paper",
  );

  function resetFromAccount(): void {
    setLabel(account.label ?? "");
    setTradingMode(account.trading_mode === "live" ? "live" : "paper");
  }

  const mutation = useMutation({
    mutationFn: async (): Promise<BrokerAccount> => {
      const token = await getToken();
      const body: BrokerAccountUpdate = {};
      // Only send changed fields. An emptied label → explicit null (clear);
      // a non-empty trimmed label → set. An unchanged field is omitted.
      const trimmedLabel = label.trim();
      const currentLabel = account.label ?? "";
      if (trimmedLabel !== currentLabel) {
        body.label = trimmedLabel === "" ? null : trimmedLabel;
      }
      if (tradingMode !== account.trading_mode) {
        body.trading_mode = tradingMode;
      }
      return updateBrokerAccount(account.id, body, token);
    },
    onSuccess: (updated) => {
      onUpdated?.(updated);
      qc.setQueryData<BrokerAccount[]>(["broker-accounts"], (prev) =>
        prev?.map((a) => (a.id === updated.id ? updated : a)),
      );
      void qc.invalidateQueries({ queryKey: ["broker-accounts"] });
      toast.success("Account updated", {
        description: `${updated.ib_account_id} — ${updated.trading_mode}${
          updated.label ? ` · ${updated.label}` : ""
        }.`,
      });
      setOpen(false);
    },
    onError: (err) => {
      toast.error("Update failed", {
        description: describeApiError(err, "Failed to update account"),
      });
    },
  });

  const hasChanges =
    label.trim() !== (account.label ?? "") ||
    tradingMode !== account.trading_mode;

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) resetFromAccount();
      }}
    >
      <Button
        variant="outline"
        size="sm"
        className="gap-2"
        disabled={disabled}
        onClick={() => {
          resetFromAccount();
          setOpen(true);
        }}
        data-testid="broker-account-edit-trigger"
      >
        <Pencil className="size-4" aria-hidden="true" />
        Edit
      </Button>
      <DialogContent data-testid="broker-account-edit-dialog">
        <DialogHeader>
          <DialogTitle>Edit account</DialogTitle>
          <DialogDescription>
            Update the label and trading mode for{" "}
            <span className="font-mono font-medium text-foreground">
              {account.ib_account_id}
            </span>
            . The IB account id and credentials are not editable here.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="edit-label">Label</Label>
            <Input
              id="edit-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="Optional human-readable label"
              autoComplete="off"
              data-testid="broker-account-edit-label"
            />
            <p className="text-xs text-muted-foreground">
              Clear the field to remove the label.
            </p>
          </div>
          <div className="space-y-2">
            <Label htmlFor="edit-trading-mode">Trading mode</Label>
            <Select
              value={tradingMode}
              onValueChange={(v) => setTradingMode(v as TradingMode)}
            >
              <SelectTrigger
                id="edit-trading-mode"
                data-testid="broker-account-edit-trading-mode"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="paper">Paper</SelectItem>
                <SelectItem value="live">Live</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <DialogFooter className="gap-2">
          <Button
            variant="outline"
            onClick={() => setOpen(false)}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <Button
            onClick={() => mutation.mutate()}
            disabled={!hasChanges || mutation.isPending}
            className="gap-2"
            data-testid="broker-account-edit-save"
          >
            <Pencil className="size-4" aria-hidden="true" />
            {mutation.isPending ? "Saving…" : "Save changes"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RotateCredentialsDialog({
  account,
  disabled,
  onUpdated,
}: {
  account: BrokerAccount;
  disabled: boolean;
  onUpdated?: (account: BrokerAccount) => void;
}): React.ReactElement {
  const { getToken } = useAuth();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [twsUserid, setTwsUserid] = useState("");
  const [twsPassword, setTwsPassword] = useState("");

  const mutation = useMutation({
    mutationFn: async (): Promise<BrokerAccount> => {
      const token = await getToken();
      return rotateBrokerAccountCredentials(
        account.id,
        { tws_userid: twsUserid, tws_password: twsPassword },
        token,
      );
    },
    onSuccess: (updated) => {
      // Reflect the rotation immediately in the open drawer (new secret
      // version / updated-at) by lifting the refreshed account to the parent's
      // selected state, and patch the list cache so the row matches too —
      // then revalidate from the server.
      onUpdated?.(updated);
      qc.setQueryData<BrokerAccount[]>(["broker-accounts"], (prev) =>
        prev?.map((a) => (a.id === updated.id ? updated : a)),
      );
      void qc.invalidateQueries({ queryKey: ["broker-accounts"] });
      toast.success("Credentials rotated", {
        description: `${account.ib_account_id} — secret version is now ${
          updated.credentials_secret_version ?? "updated"
        }.`,
      });
      setOpen(false);
      setTwsUserid("");
      setTwsPassword("");
    },
    onError: (err) => {
      toast.error("Rotation failed", {
        description: describeApiError(err, "Failed to rotate credentials"),
      });
    },
  });

  const canSubmit = twsUserid.trim() !== "" && twsPassword !== "";

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) {
          setTwsUserid("");
          setTwsPassword("");
        }
      }}
    >
      <Button
        variant="outline"
        size="sm"
        className="gap-2"
        disabled={disabled}
        onClick={() => setOpen(true)}
        data-testid="broker-account-rotate-trigger"
      >
        <KeyRound className="size-4" aria-hidden="true" />
        Rotate credentials
      </Button>
      <DialogContent data-testid="broker-account-rotate-dialog">
        <DialogHeader>
          <DialogTitle>Rotate credentials</DialogTitle>
          <DialogDescription>
            Replace the stored TWS credentials for{" "}
            <span className="font-mono font-medium text-foreground">
              {account.ib_account_id}
            </span>
            . The secret version advances; the previous secret is superseded.
            The values you type here are written straight to the secrets backend
            and never displayed again.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="rotate-tws-userid">TWS user id</Label>
            <Input
              id="rotate-tws-userid"
              value={twsUserid}
              onChange={(e) => setTwsUserid(e.target.value)}
              autoComplete="off"
              data-testid="broker-account-rotate-userid"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="rotate-tws-password">TWS password</Label>
            <Input
              id="rotate-tws-password"
              type="password"
              value={twsPassword}
              onChange={(e) => setTwsPassword(e.target.value)}
              autoComplete="new-password"
              data-testid="broker-account-rotate-password"
            />
          </div>
        </div>
        <DialogFooter className="gap-2">
          <Button
            variant="outline"
            onClick={() => setOpen(false)}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <Button
            onClick={() => mutation.mutate()}
            disabled={!canSubmit || mutation.isPending}
            className="gap-2"
            data-testid="broker-account-rotate-confirm"
          >
            <KeyRound className="size-4" aria-hidden="true" />
            {mutation.isPending ? "Rotating…" : "Rotate"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ArchiveDialog({
  account,
  disabled,
  onArchived,
}: {
  account: BrokerAccount;
  disabled: boolean;
  onArchived: () => void;
}): React.ReactElement {
  const { getToken } = useAuth();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);

  // Migrated (legacy_env) accounts keep their credentials in the compose
  // environment, so archive() intentionally does NOT delete a stored secret for
  // them — don't promise deletion in the operator copy (Codex review).
  const isLegacyEnv = account.credentials_backend === "legacy_env";

  const mutation = useMutation({
    mutationFn: async (): Promise<BrokerAccount> => {
      const token = await getToken();
      return archiveBrokerAccount(account.id, token);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["broker-accounts"] });
      toast.success("Account archived", {
        description: isLegacyEnv
          ? `${account.ib_account_id} — slot ${account.gateway_slot} freed. (Environment-managed credentials are unchanged.)`
          : `${account.ib_account_id} — slot ${account.gateway_slot} freed and stored secret deleted.`,
      });
      setOpen(false);
      onArchived();
    },
    onError: (err) => {
      toast.error("Archive failed", {
        description: describeApiError(err, "Failed to archive account"),
      });
    },
  });

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <Button
        variant="destructive"
        size="sm"
        className="gap-2"
        disabled={disabled}
        onClick={() => setOpen(true)}
        data-testid="broker-account-archive-trigger"
      >
        <Archive className="size-4" aria-hidden="true" />
        Archive
      </Button>
      <AlertDialogContent data-testid="broker-account-archive-dialog">
        <AlertDialogHeader>
          <AlertDialogTitle>Archive this broker account?</AlertDialogTitle>
          <AlertDialogDescription>
            Archiving{" "}
            <span className="font-mono font-medium text-foreground">
              {account.ib_account_id}
            </span>{" "}
            frees gateway slot{" "}
            <span className="font-mono font-medium text-foreground">
              {account.gateway_slot}
            </span>{" "}
            {isLegacyEnv
              ? "(its credentials live in the environment configuration and are left unchanged)"
              : "and deletes the stored secret"}
            . This account can no longer trade after archiving.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={mutation.isPending}>
            Cancel
          </AlertDialogCancel>
          <AlertDialogAction
            onClick={(e) => {
              e.preventDefault();
              mutation.mutate();
            }}
            disabled={mutation.isPending}
            className="bg-red-500/90 text-red-50 hover:bg-red-500"
            data-testid="broker-account-archive-confirm"
          >
            {mutation.isPending ? "Archiving…" : "Archive account"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
