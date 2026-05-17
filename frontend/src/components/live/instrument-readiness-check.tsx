"use client";

import { useEffect, useMemo } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  HelpCircle,
} from "lucide-react";
import { getInventory, type InventoryRow } from "@/lib/api";
import { useAuth } from "@/lib/auth";

interface Props {
  /** Raw comma-separated instrument text from the compose form. */
  instrumentsText: string;
  /**
   * Notifies the parent whether all instruments resolve cleanly (resolved
   * to exactly one inventory row each, no ambiguity). Used to gate the
   * Add Member button per R16.
   */
  onValidityChange?: (allClear: boolean) => void;
}

/**
 * InstrumentReadinessCheck — per R16, blocks the compose flow whenever
 * any instrument in the textarea is NOT in the registry OR matches
 * multiple inventory rows across asset classes.
 *
 * Each entered string is matched against `getInventory()` by symbol.
 * Matches with exactly 1 row are "resolved." 0 matches → "missing"
 * (with onboard CTA). >1 matches → "ambiguous" (with explicit warning).
 */
export function InstrumentReadinessCheck({
  instrumentsText,
  onValidityChange,
}: Props): React.ReactElement | null {
  const { getToken, isAuthenticated } = useAuth();

  const query = useQuery<InventoryRow[], Error>({
    queryKey: ["inventory", "readiness"],
    queryFn: async (): Promise<InventoryRow[]> => {
      const token = await getToken();
      return getInventory(token);
    },
    enabled: isAuthenticated,
    staleTime: 60_000,
    retry: 1,
  });

  const instruments = useMemo(
    () => parseInstruments(instrumentsText),
    [instrumentsText],
  );

  const verdict = useMemo(
    () => resolve(instruments, query.data ?? []),
    [instruments, query.data],
  );

  // Codex iter-1 P2 + SF F3: indeterminate state. While inventory query
  // is pending or errored AND the user has typed at least one instrument,
  // emit ``false`` so Add Member stays disabled. Old code defaulted to
  // letting the form submit during the pending window OR claimed the
  // form would not block on error (which contradicted the actual gate).
  useEffect(() => {
    if (!onValidityChange) return;
    if (instruments.length === 0) {
      onValidityChange(true);
      return;
    }
    if (query.isPending || query.isError) {
      onValidityChange(false);
      return;
    }
    onValidityChange(verdict.allClear);
  }, [
    verdict.allClear,
    query.isPending,
    query.isError,
    instruments.length,
    onValidityChange,
  ]);

  if (instruments.length === 0) return null;

  if (query.isPending) {
    return (
      <ReadinessCard tone="info" icon={HelpCircle}>
        Checking registry… Add Member is blocked until validation completes.
      </ReadinessCard>
    );
  }

  if (query.isError) {
    return (
      <ReadinessCard tone="error" icon={AlertTriangle}>
        Failed to load instrument registry — Add Member is blocked until the
        registry is reachable. Reload, or check{" "}
        <Link href="/market-data" className="underline">
          market data
        </Link>
        .
      </ReadinessCard>
    );
  }

  if (verdict.allClear) {
    return (
      <ReadinessCard tone="ok" icon={CheckCircle2}>
        All {instruments.length}{" "}
        {instruments.length === 1
          ? "instrument resolves"
          : "instruments resolve"}{" "}
        cleanly against the registry.
      </ReadinessCard>
    );
  }

  return (
    <div className="space-y-3 rounded-md border border-amber-500/30 bg-amber-500/10 p-3">
      <div className="flex items-start gap-2">
        <AlertTriangle
          className="mt-0.5 size-4 shrink-0 text-amber-400"
          aria-hidden="true"
        />
        <p className="text-sm text-amber-400">
          {verdict.missing.length + verdict.ambiguous.length} unresolved
          {verdict.missing.length + verdict.ambiguous.length === 1
            ? " instrument"
            : " instruments"}{" "}
          — Add / Snapshot / Start blocked until resolved.
        </p>
      </div>

      {verdict.missing.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs uppercase tracking-wide text-muted-foreground">
            Not in registry
          </p>
          <ul className="space-y-2">
            {verdict.missing.map((sym) => (
              <li
                key={sym}
                className="flex items-center justify-between gap-3 rounded-md border border-border/50 bg-card/40 p-2 text-sm"
              >
                <Badge variant="outline" className="font-mono">
                  {sym}
                </Badge>
                <Button asChild variant="outline" size="sm" className="gap-2">
                  <Link
                    href={`/market-data?onboard=${encodeURIComponent(sym)}`}
                  >
                    <ExternalLink className="size-3.5" aria-hidden="true" />
                    Onboard via Market Data
                  </Link>
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {verdict.ambiguous.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs uppercase tracking-wide text-muted-foreground">
            Multiple matches (pick one asset class)
          </p>
          <ul className="space-y-2">
            {verdict.ambiguous.map(({ symbol, rows }) => (
              <li
                key={symbol}
                className="space-y-1 rounded-md border border-border/50 bg-card/40 p-2 text-sm"
              >
                <Badge variant="outline" className="font-mono">
                  {symbol}
                </Badge>
                <p className="text-xs text-muted-foreground">
                  Matches:{" "}
                  {rows.map((r) => `${r.symbol}.${r.asset_class}`).join(", ")} —
                  rename the instrument string to{" "}
                  <code className="font-mono">SYMBOL.ASSET_CLASS</code> to
                  disambiguate (e.g.{" "}
                  <code className="font-mono">
                    {rows[0].symbol}.{rows[0].asset_class}
                  </code>
                  ).
                </p>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ReadinessCard({
  tone,
  icon: Icon,
  children,
}: {
  tone: "ok" | "info" | "error";
  icon: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
}): React.ReactElement {
  const className =
    tone === "ok"
      ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-400"
      : tone === "info"
        ? "border-border/50 bg-card/40 text-muted-foreground"
        : "border-red-500/30 bg-red-500/10 text-red-400";
  return (
    <div
      className={`flex items-start gap-2 rounded-md border p-3 text-sm ${className}`}
      role={tone === "error" ? "alert" : undefined}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="flex-1">{children}</div>
    </div>
  );
}

interface Verdict {
  allClear: boolean;
  missing: string[];
  ambiguous: { symbol: string; rows: InventoryRow[] }[];
}

// Known asset-class suffixes — used to distinguish a disambiguation
// suffix (``AAPL.equity``) from a symbol that legitimately contains a
// dot (``BRK.B`` / ``ES.c.0``). Codex iter-2 P2 #1.
const KNOWN_ASSET_CLASS_SUFFIXES = new Set([
  "equity",
  "futures",
  "fx",
  "option",
]);

function resolve(instruments: string[], inventory: InventoryRow[]): Verdict {
  const missing: string[] = [];
  const ambiguous: { symbol: string; rows: InventoryRow[] }[] = [];

  for (const instr of instruments) {
    // Disambiguation syntax: ``SYMBOL.equity`` / ``SYMBOL.futures`` /
    // ``SYMBOL.fx`` / ``SYMBOL.option``. Only split when the trailing
    // segment is a known asset-class — otherwise dotted symbols like
    // ``BRK.B`` and ``ES.c.0`` would be misparsed and marked missing.
    const lastDot = instr.lastIndexOf(".");
    const trailingSegment = lastDot >= 0 ? instr.slice(lastDot + 1) : "";
    const explicitTuple =
      lastDot > 0 &&
      KNOWN_ASSET_CLASS_SUFFIXES.has(trailingSegment.toLowerCase())
        ? [instr.slice(0, lastDot), trailingSegment.toLowerCase()]
        : null;
    const matches = inventory.filter((row) => {
      if (explicitTuple) {
        return (
          row.symbol === explicitTuple[0] &&
          row.asset_class === explicitTuple[1]
        );
      }
      return row.symbol === instr;
    });
    if (matches.length === 0) {
      missing.push(instr);
    } else if (matches.length > 1) {
      ambiguous.push({ symbol: instr, rows: matches });
    }
  }

  return {
    allClear: missing.length === 0 && ambiguous.length === 0,
    missing,
    ambiguous,
  };
}

function parseInstruments(text: string): string[] {
  return text
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}
