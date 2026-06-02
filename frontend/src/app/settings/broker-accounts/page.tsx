"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Plus } from "lucide-react";

import { BrokerAccountsTable } from "@/components/broker-accounts/broker-accounts-table";
import { BrokerAccountDetail } from "@/components/broker-accounts/broker-account-detail";
import { BrokerAccountWizard } from "@/components/broker-accounts/broker-account-wizard";
import {
  listBrokerAccounts,
  type BrokerAccount,
} from "@/lib/api/broker-accounts";
import { describeApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

/**
 * Settings → Broker accounts.
 *
 * Lists the IB accounts in the multi-account fleet (control plane). Each row
 * opens a detail drawer with credential METADATA only (the secret never
 * leaves the secrets backend) plus rotate / archive actions.
 *
 * The "Add account" button (header + empty state) opens the add-account
 * wizard (T16). Editing an existing account (label / trading_mode) and
 * rotating its credentials live in the detail drawer, which never
 * round-trips the secret.
 */
export default function BrokerAccountsPage(): React.ReactElement {
  const { getToken, isAuthenticated } = useAuth();
  const [selected, setSelected] = useState<BrokerAccount | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);

  const accountsQuery = useQuery<BrokerAccount[], Error>({
    queryKey: ["broker-accounts"],
    queryFn: async (): Promise<BrokerAccount[]> => {
      const token = await getToken();
      return listBrokerAccounts(token);
    },
    enabled: isAuthenticated,
    staleTime: 30_000,
  });

  const handleAdd = (): void => {
    setWizardOpen(true);
  };

  const handleSelect = (account: BrokerAccount): void => {
    setSelected(account);
    setDetailOpen(true);
  };

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Broker accounts
          </h1>
          <p className="text-sm text-muted-foreground">
            Interactive Brokers accounts in the fleet. Each account owns a
            gateway slot and trades through its own credentials — stored in the
            secrets backend, never shown here.
          </p>
        </div>
        <Button
          variant="default"
          size="sm"
          className="gap-2"
          onClick={handleAdd}
          data-testid="broker-accounts-add"
        >
          <Plus className="size-4" aria-hidden="true" />
          Add account
        </Button>
      </div>

      <BrokerAccountsTable
        accounts={accountsQuery.data ?? []}
        isLoading={accountsQuery.isPending && isAuthenticated}
        error={
          accountsQuery.isError
            ? describeApiError(
                accountsQuery.error,
                "Failed to load broker accounts",
              )
            : null
        }
        onSelect={handleSelect}
        onAdd={handleAdd}
      />

      <BrokerAccountDetail
        account={selected}
        open={detailOpen}
        onOpenChange={setDetailOpen}
        onUpdated={setSelected}
      />

      <BrokerAccountWizard open={wizardOpen} onOpenChange={setWizardOpen} />
    </div>
  );
}
