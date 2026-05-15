import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { LiveStopResponse, KillAllFlatnessReport } from "@/lib/api";

/**
 * Discriminated-union prop set. The same component is used by:
 *   - the stop dialog (kind="stop") — full LiveStopResponse
 *   - the kill-all panel  (kind="kill-all-entry") — one KillAllFlatnessReport
 */
export type FlatnessProps =
  | { kind: "stop"; data: LiveStopResponse }
  | { kind: "kill-all-entry"; data: KillAllFlatnessReport };

interface NormalizedFlatness {
  brokerFlat: boolean | null | undefined;
  remainingPositions: Array<Record<string, unknown>> | undefined;
  stopNonce: string | null | undefined;
  processStatus: string | undefined;
}

function normalize(props: FlatnessProps): NormalizedFlatness {
  if (props.kind === "stop") {
    return {
      brokerFlat: props.data.broker_flat,
      remainingPositions: props.data.remaining_positions,
      stopNonce: props.data.stop_nonce,
      processStatus: props.data.process_status,
    };
  }
  return {
    brokerFlat: props.data.broker_flat,
    remainingPositions: props.data.remaining_positions,
    stopNonce: props.data.stop_nonce,
    processStatus: undefined,
  };
}

function flatnessBadge(
  brokerFlat: boolean | null | undefined,
): React.ReactElement {
  if (brokerFlat === true) {
    return (
      <Badge
        data-testid="flatness-badge"
        variant="secondary"
        className="bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/25 dark:bg-emerald-500/15 dark:text-emerald-400"
      >
        FLAT
      </Badge>
    );
  }
  if (brokerFlat === false) {
    return (
      <Badge
        data-testid="flatness-badge"
        variant="secondary"
        className="bg-red-500/20 text-red-300 hover:bg-red-500/25 dark:bg-red-500/15 dark:text-red-400"
      >
        NOT FLAT — POSITIONS OPEN
      </Badge>
    );
  }
  // null or undefined → UNKNOWN (timed out / never arrived). Safety memo:
  // operator must verify via IB portal.
  return (
    <Badge
      data-testid="flatness-badge"
      variant="secondary"
      className="bg-orange-500/20 text-orange-300 hover:bg-orange-500/25 dark:bg-orange-500/15 dark:text-orange-400"
    >
      UNKNOWN — verify in IB portal
    </Badge>
  );
}

function cellOrDash(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

export function FlatnessDisplay(props: FlatnessProps): React.ReactElement {
  const { brokerFlat, remainingPositions, stopNonce, processStatus } =
    normalize(props);

  // "Already stopped" idempotent path: nothing meaningful to render.
  if (brokerFlat === undefined && remainingPositions === undefined) {
    return (
      <p className="text-sm text-muted-foreground">
        No stop report available (deployment was already stopped).
      </p>
    );
  }

  const hasPositions =
    Array.isArray(remainingPositions) && remainingPositions.length > 0;

  return (
    <section className="space-y-3" aria-label="Broker flatness report">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-foreground">
          Broker flatness:
        </span>
        {flatnessBadge(brokerFlat)}
      </div>

      {hasPositions ? (
        <div className="rounded-md border border-border/50">
          <Table data-testid="flatness-positions-table">
            <TableHeader>
              <TableRow className="border-border/50 hover:bg-transparent">
                <TableHead>Instrument</TableHead>
                <TableHead>Qty</TableHead>
                <TableHead>Side</TableHead>
                <TableHead>Avg Price</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {remainingPositions!.map((pos, idx) => {
                const instrument = pos.instrument_id ?? pos.instrument;
                return (
                  <TableRow
                    key={`${cellOrDash(instrument)}-${idx}`}
                    className="border-border/50"
                  >
                    <TableCell className="font-mono text-xs">
                      {cellOrDash(instrument)}
                    </TableCell>
                    {/* Codex code-review P2: backend writes `quantity`,
                        not `qty` (see trading_node_subprocess.py). Read
                        both so any other producer is forward-compatible. */}
                    <TableCell>{cellOrDash(pos.quantity ?? pos.qty)}</TableCell>
                    <TableCell>{cellOrDash(pos.side)}</TableCell>
                    <TableCell>{cellOrDash(pos.avg_price)}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      ) : null}

      {stopNonce || processStatus ? (
        <p className="font-mono text-xs text-muted-foreground">
          {stopNonce ? <>nonce: {stopNonce}</> : null}
          {stopNonce && processStatus ? " · " : null}
          {processStatus ? <>process: {processStatus}</> : null}
        </p>
      ) : null}
    </section>
  );
}
