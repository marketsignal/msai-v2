"use client";

/**
 * `<RunSmokeButton />` — header action on `/backtests` that POSTs to
 * `/api/v1/portfolios/smoke/runs?config=fast` and surfaces the resulting
 * `PortfolioRun.id` via a sonner toast.
 *
 * Auth pattern matches the project's existing authenticated-mutation
 * components (e.g., `components/live/resume-button.tsx`):
 *   const { getToken } = useAuth();   // from "@/lib/auth"
 *   const token = await getToken();
 *   await apiPost("/path", body, token);
 *
 * Note: `apiPost`'s third argument is a positional `token?: string | null`
 * — NOT an `{ token }` options bag. See `frontend/src/lib/api.ts`.
 *
 * The endpoint is authenticated via FastAPI's `get_current_user`. In dev
 * the browser uses `NEXT_PUBLIC_MSAI_API_KEY` as fallback when MSAL has no
 * token; in prod the bearer token is obtained via `useAuth().getToken()`.
 */

import * as React from "react";
import { Loader2, Zap } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { apiPost, describeApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

interface SmokeRunResponse {
  id: string;
  portfolio_id: string;
  status: string;
}

export function RunSmokeButton(): React.ReactElement {
  const { getToken } = useAuth();
  const [pending, setPending] = React.useState<boolean>(false);

  const handleClick = async (): Promise<void> => {
    setPending(true);
    try {
      const token = await getToken();
      // POST body is null — config is selected via the `config` query string.
      const data = await apiPost<SmokeRunResponse>(
        "/api/v1/portfolios/smoke/runs?config=fast",
        null,
        token,
      );
      toast.success("Smoke run started", {
        description: `Run id: ${data.id}`,
      });
    } catch (err) {
      toast.error(describeApiError(err, "Failed to start smoke"));
      console.error("Run smoke failed:", err);
    } finally {
      setPending(false);
    }
  };

  return (
    <Button
      data-testid="run-smoke-button"
      type="button"
      variant="secondary"
      onClick={handleClick}
      disabled={pending}
      className="gap-2"
    >
      {pending ? (
        <Loader2 className="size-4 animate-spin motion-reduce:hidden" />
      ) : (
        <Zap className="size-4" aria-hidden="true" />
      )}
      {pending ? "Starting…" : "Run smoke"}
    </Button>
  );
}
