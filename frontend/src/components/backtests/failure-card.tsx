"use client";

import { useEffect, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Copy, Check } from "lucide-react";
import type { ErrorEnvelope } from "@/lib/api";

interface FailureCardProps {
  error: ErrorEnvelope;
}

export function FailureCard({ error }: FailureCardProps): React.ReactElement {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  // Refs so rapid double-clicks and unmounts don't flip stale state.
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const errorTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
      if (errorTimer.current) clearTimeout(errorTimer.current);
    };
  }, []);

  const onCopy = async (): Promise<void> => {
    if (!error.suggested_action) return;
    // navigator.clipboard is undefined on insecure origins
    // (non-HTTPS, non-localhost) and can throw a DOMException on
    // permissions-policy denial. Surface a user-visible error instead
    // of silently failing.
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error("Clipboard API unavailable");
      }
      await navigator.clipboard.writeText(error.suggested_action);
      setCopied(true);
      setCopyError(null);
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
      copiedTimer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopyError("Copy failed — select the command and copy manually.");
      if (errorTimer.current) clearTimeout(errorTimer.current);
      errorTimer.current = setTimeout(() => setCopyError(null), 4000);
    }
  };

  return (
    <Card
      className="border-red-500/30 bg-red-500/5"
      data-testid="backtest-failure-card"
    >
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Badge
            variant="secondary"
            className="font-mono text-xs"
            data-testid="backtest-error-code"
          >
            {error.code.toUpperCase()}
          </Badge>
          <span>Backtest failed</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p
          className="whitespace-pre-wrap text-sm text-muted-foreground"
          data-testid="backtest-error-message"
        >
          {error.message}
        </p>

        {error.suggested_action && (
          <div className="space-y-2">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Suggested action
            </p>
            <div className="flex items-start gap-2">
              <pre
                className="flex-1 overflow-x-auto rounded-md bg-muted p-3 font-mono text-xs"
                data-testid="backtest-error-suggested-action"
              >
                <code>{error.suggested_action}</code>
              </pre>
              <Button
                variant="outline"
                size="icon"
                onClick={() => void onCopy()}
                aria-label="Copy command"
                data-testid="backtest-error-copy-button"
              >
                {copied ? (
                  <Check className="size-3.5" />
                ) : (
                  <Copy className="size-3.5" />
                )}
              </Button>
            </div>
            {copyError && (
              <p
                className="text-xs text-amber-500"
                role="status"
                data-testid="backtest-error-copy-error"
              >
                {copyError}
              </p>
            )}
          </div>
        )}

        {error.remediation && error.remediation.kind === "ingest_data" && (
          <div className="space-y-1 rounded-md border border-border/50 p-3 text-xs">
            <p className="font-medium">Remediation details</p>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-muted-foreground">
              {error.remediation.symbols && (
                <>
                  <dt>Symbols:</dt>
                  <dd className="font-mono">
                    {error.remediation.symbols.join(", ")}
                  </dd>
                </>
              )}
              {error.remediation.asset_class && (
                <>
                  <dt>Asset class:</dt>
                  <dd>{error.remediation.asset_class}</dd>
                </>
              )}
              {error.remediation.start_date && (
                <>
                  <dt>Date range:</dt>
                  <dd>
                    {error.remediation.start_date}
                    {" → "}
                    {error.remediation.end_date}
                  </dd>
                </>
              )}
            </dl>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
