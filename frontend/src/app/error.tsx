"use client";

import { useEffect } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { AlertTriangle, RotateCw, ArrowLeft } from "lucide-react";

interface ErrorPageProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function ErrorPage({
  error,
  reset,
}: ErrorPageProps): React.ReactElement {
  useEffect(() => {
    // Surface in dev console + lets monitoring middleware capture if wired.
    console.error("Unhandled render-time error:", error);
  }, [error]);

  return (
    <div className="flex min-h-[60vh] items-center justify-center px-4">
      <Card className="max-w-md border-red-500/30">
        <CardContent className="flex flex-col items-center gap-4 p-8 text-center">
          <div className="flex size-12 items-center justify-center rounded-md bg-primary">
            <span className="text-lg font-bold text-primary-foreground">M</span>
          </div>
          <AlertTriangle className="size-12 text-red-400" aria-hidden="true" />
          <div className="space-y-2">
            <h1 className="text-2xl font-semibold tracking-tight">
              Something went wrong
            </h1>
            <p className="text-sm text-muted-foreground">
              An unexpected error interrupted this page. You can retry, or head
              back to the dashboard.
            </p>
            {error.digest && (
              <p className="font-mono text-xs text-muted-foreground/70">
                Reference: {error.digest}
              </p>
            )}
          </div>
          <div className="flex gap-2">
            <Button onClick={reset} className="gap-2">
              <RotateCw className="size-4" aria-hidden="true" />
              Retry
            </Button>
            <Button asChild variant="outline" className="gap-2">
              <Link href="/dashboard">
                <ArrowLeft className="size-4" aria-hidden="true" />
                Dashboard
              </Link>
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
