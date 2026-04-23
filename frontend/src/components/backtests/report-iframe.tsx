"use client";

import { useEffect, useState } from "react";
import { AlertCircle, Loader2 } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { ApiError, getBacktestReportToken } from "@/lib/api";
import { useAuth } from "@/lib/auth";

/**
 * Map ``ApiError.body.error.code`` to user-facing copy. Falls back to a
 * generic message when the error isn't structured (network partition,
 * non-JSON 5xx, etc.).
 */
function apiErrorToReportCopy(e: unknown): string {
  if (e instanceof ApiError) {
    const body = e.body as { error?: { code?: string } } | undefined;
    const code = body?.error?.code;
    if (code === "INVALID_TOKEN") {
      return "Report link expired — switch tabs to reload.";
    }
    if (code === "NO_REPORT") {
      return "No QuantStats report was generated for this backtest.";
    }
    if (code === "REPORT_FILE_MISSING") {
      return "Report file was removed from disk. Re-run the backtest.";
    }
    if (code === "FORBIDDEN") {
      return "Not authorized to view this report.";
    }
    if (code === "TOKEN_SUB_MISMATCH") {
      return "This report link was minted for a different session — refresh and try again.";
    }
    return `Report unavailable (HTTP ${e.status}).`;
  }
  return e instanceof Error ? e.message : "Failed to load report.";
}

interface ReportIframeProps {
  backtestId: string;
  hasReport: boolean;
}

/**
 * Embeds the QuantStats tear-sheet HTML in an iframe. Authenticates via the
 * stateless signed-URL flow: the backend mints a short-lived
 * HMAC-signed URL scoped to ``(backtest_id, user_sub, exp)``; the iframe
 * uses it directly as ``src``. No Next.js proxy, no server-side API key.
 *
 * The URL expires after ``settings.report_token_ttl_seconds`` (default 60s).
 * Re-mounts of this component (e.g. tab switches) trigger a fresh token so
 * an already-expired URL auto-refreshes the next time the user opens the tab.
 */
export function ReportIframe({
  backtestId,
  hasReport,
}: ReportIframeProps): React.JSX.Element {
  const { getToken } = useAuth();
  const [signedUrl, setSignedUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!hasReport) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    const fetchToken = async (): Promise<void> => {
      setLoading(true);
      setError(null);
      try {
        const token = await getToken();
        const res = await getBacktestReportToken(backtestId, token);
        if (cancelled) return;
        setSignedUrl(res.signed_url);
      } catch (e: unknown) {
        if (!cancelled) {
          setError(apiErrorToReportCopy(e));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void fetchToken();
    return () => {
      cancelled = true;
    };
  }, [backtestId, hasReport, getToken]);

  if (!hasReport) {
    return (
      <Card className="border-border/50">
        <CardContent className="flex flex-col items-center justify-center gap-3 py-12 text-muted-foreground">
          <AlertCircle className="h-8 w-8" />
          <p className="text-sm">
            Full report not available for this backtest.
          </p>
          <p className="text-xs">
            Switch to Native view to see populated charts.
          </p>
        </CardContent>
      </Card>
    );
  }

  if (loading) {
    return (
      <div
        className="flex h-[900px] items-center justify-center rounded-lg border border-border/50"
        role="status"
        aria-label="Loading tear sheet"
      >
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (error !== null || signedUrl === null) {
    return (
      <Card className="border-destructive/50" data-testid="report-iframe-error">
        <CardContent className="flex flex-col items-center justify-center gap-3 py-12 text-destructive">
          <AlertCircle className="h-8 w-8" aria-hidden="true" />
          <p className="text-sm">{error ?? "Unable to load report."}</p>
        </CardContent>
      </Card>
    );
  }

  // Backend returns an API-relative path like
  // ``/api/v1/backtests/{id}/report?token=<hmac>``. In split-origin deployments
  // (frontend host ≠ backend host) the iframe would otherwise resolve against
  // the frontend origin and 404. Prepend NEXT_PUBLIC_API_URL (same base
  // ``apiFetch`` uses) so the iframe points at the backend.
  const apiBase = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8800";
  const absoluteSignedUrl = signedUrl.startsWith("http")
    ? signedUrl
    : `${apiBase}${signedUrl}`;

  return (
    <div className="relative h-[900px] w-full overflow-hidden rounded-lg border border-border/50">
      <iframe
        src={absoluteSignedUrl}
        className="h-full w-full"
        title="QuantStats tear sheet"
        // Sandbox: scripts + same-origin are required for Plotly (the chart
        // engine QuantStats emits) — its scripts read their own runtime from
        // the document. Sandbox without these flags loads a blank frame.
        sandbox="allow-scripts allow-same-origin"
      />
    </div>
  );
}
