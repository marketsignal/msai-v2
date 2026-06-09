"use client";

import { createContext, useContext } from "react";
import { useLocalStorage } from "usehooks-ts";

/** The selected global account scope. "all" = no filter (default);
 * "unassigned" = legacy deployments with no account_id; otherwise a concrete
 * ib_account_id. View-scope ONLY — never a deploy target (PRD US-001/US-002). */
export type AccountScope = string; // "all" | "unassigned" | <ib_account_id>

interface AccountScopeValue {
  scope: AccountScope;
  setScope: (next: AccountScope) => void;
}

const AccountScopeContext = createContext<AccountScopeValue | null>(null);
const STORAGE_KEY = "msai.accountScope";

export function AccountScopeProvider({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement {
  // initializeWithValue:false forces the default ("all") on the server AND the
  // first client render, then syncs the persisted value after mount — this is
  // the explicit hydration-safe form (iter-1 P2#7: the bare 2-arg call can read
  // the persisted client value on first render → hydration mismatch).
  const [scope, setScope] = useLocalStorage<AccountScope>(STORAGE_KEY, "all", {
    initializeWithValue: false,
  });
  return (
    <AccountScopeContext.Provider value={{ scope, setScope }}>
      {children}
    </AccountScopeContext.Provider>
  );
}

export function useAccountScope(): AccountScopeValue {
  const ctx = useContext(AccountScopeContext);
  if (!ctx)
    throw new Error("useAccountScope must be used within AccountScopeProvider");
  return ctx;
}
