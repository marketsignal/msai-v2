"use client";

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useAccountScope } from "@/lib/account-scope";
import { useAuth } from "@/lib/auth";
import { getLiveStatus } from "@/lib/api";
import {
  listBrokerAccounts,
  type BrokerAccount,
} from "@/lib/api/broker-accounts";

interface Option {
  value: string;
  label: string;
  realMoney: boolean;
  unknown?: boolean;
}

function buildOptions(
  accounts: BrokerAccount[],
  deploymentAccountIds: (string | null)[],
  registryLoaded: boolean,
): Option[] {
  const opts: Option[] = [
    { value: "all", label: "All accounts", realMoney: false },
  ];
  const registered = new Set<string>();
  for (const a of accounts) {
    registered.add(a.ib_account_id);
    opts.push({
      value: a.ib_account_id,
      label: a.is_real_money
        ? `REAL FUND — ${a.label ?? a.ib_account_id} (${a.ib_account_id}) — LIVE MONEY`
        : `${a.label ?? a.ib_account_id} (${a.ib_account_id})`,
      realMoney: a.is_real_money,
    });
  }
  // Codex code-review iter-6 P2: only classify a deployment-only account as
  // "Unknown/retired" once the REGISTRY query has actually succeeded. If
  // /broker-accounts is still loading or errored, `accounts` is empty and EVERY
  // deployment account would otherwise render as Unknown/retired (realMoney
  // false) — stripping a real fund's unmistakable REAL FUND label. While the
  // registry is unavailable we simply omit deployment-only unknowns (the
  // account reappears with correct labeling once the registry loads; the
  // trigger shows a load-failure hint — see component).
  if (registryLoaded) {
    for (const id of deploymentAccountIds) {
      if (id && !registered.has(id)) {
        opts.push({
          value: id,
          label: `Unknown/retired account ${id}`,
          realMoney: false,
          unknown: true,
        });
        registered.add(id);
      }
    }
  }
  if (deploymentAccountIds.some((id) => id == null)) {
    opts.push({
      value: "unassigned",
      label: "Unassigned (legacy)",
      realMoney: false,
    });
  }
  return opts;
}

export function AccountSelector(): React.ReactElement | null {
  const { scope, setScope } = useAccountScope();
  const { getToken } = useAuth();
  const accountsQ = useQuery({
    queryKey: ["broker-accounts"],
    queryFn: async () => listBrokerAccounts(await getToken()),
  });
  const statusQ = useQuery({
    queryKey: ["live-status-accounts"],
    queryFn: async () => getLiveStatus(await getToken()),
  });
  // Codex code-review iter-8 P2: ALSO pull the uncapped active-only set and
  // MERGE its account ids. The default /live/status is capped to 50 recent rows,
  // so a long-running active deployment for a deployment-only/legacy account can
  // be pushed out by newer stopped rows — leaving the selector without the only
  // source for that account id, which makes the reconciliation effect below
  // wrongly reset that scope to "All". The default set still contributes
  // recently-STOPPED accounts (Unknown/retired); the active set guarantees no
  // ACTIVE account is missed.
  const activeStatusQ = useQuery({
    queryKey: ["live-status-accounts-active"],
    queryFn: async () => getLiveStatus(await getToken(), { activeOnly: true }),
  });
  const accounts = accountsQ.data ?? [];
  const deploymentAccountIds = [
    ...(statusQ.data?.deployments ?? []),
    ...(activeStatusQ.data?.deployments ?? []),
  ].map((d) => d.account_id ?? null);
  const options = buildOptions(
    accounts,
    deploymentAccountIds,
    accountsQ.isSuccess,
  );

  // iter-1 P1#3 / iter-5 P1: reconcile PRD US-001 ("selected account later
  // archived → fall back to All; no crash") with US-004 ("unknown/retired
  // deployment-only ids shown explicitly, not folded into All"). The reset
  // rule: reset to "all" ONLY when the persisted scope is absent from the
  // ENTIRE option union (registered-active ∪ deployment-seen ∪ all/unassigned)
  // — i.e. the account fully vanished (no registry row AND no deployment
  // references it). An account that was archived but STILL has deployment
  // history remains a valid "Unknown/retired account <id>" option (US-004) — the
  // operator can keep viewing its terminal deployments; it does NOT reset. Gate
  // on isSuccess of ALL THREE queries so a transient fetch error never wipes a
  // valid selection, and so the union is COMPLETE before we judge a scope
  // "gone" (iter-8 P2: resetting before the active-only set loaded could drop a
  // valid active scope to All).
  const optionsReady =
    accountsQ.isSuccess && statusQ.isSuccess && activeStatusQ.isSuccess;
  useEffect(() => {
    if (!optionsReady) return;
    if (!options.some((o) => o.value === scope)) setScope("all");
  }, [optionsReady, options, scope, setScope]);

  return (
    <Select value={scope} onValueChange={setScope}>
      <SelectTrigger
        data-testid="account-scope-selector"
        aria-label="Account scope"
        // Codex code-review iter-6 P2: when the registry fetch fails, the option
        // list is incomplete (deployment-only accounts are NOT classified) —
        // surface it on the trigger so the operator knows the account list may
        // be partial, rather than silently trusting it.
        title={
          accountsQ.isError
            ? "Account registry failed to load — the list may be incomplete"
            : undefined
        }
        className={
          accountsQ.isError
            ? "h-8 w-[16rem] border-destructive/50 text-sm"
            : "h-8 w-[16rem] text-sm"
        }
      >
        <SelectValue placeholder="All accounts" />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem
            key={o.value}
            value={o.value}
            data-testid={`account-scope-option-${o.value}`}
            className={
              o.realMoney ? "font-semibold text-destructive" : undefined
            }
          >
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
