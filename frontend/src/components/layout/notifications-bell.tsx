"use client";

import Link from "next/link";
import { Bell } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useAlerts } from "@/lib/hooks/use-alerts";

const TWENTY_FOUR_HOURS_MS = 24 * 60 * 60 * 1_000;

/**
 * Header alerts bell.
 *
 * Per R6 + the polling cheat-sheet, the badge shows the count of alerts
 * in the LAST 24 HOURS — not "unread," because the backend's AlertRecord
 * has no `read_at` field today. Tooltip text matches this rule so the
 * affordance isn't misleading.
 */
export function NotificationsBell(): React.ReactElement {
  const query = useAlerts(50);

  const count = countRecentAlerts(query.data?.alerts ?? []);
  const hasAlerts = count > 0;
  // SF F6: distinguish "no alerts" from "alerts feed unreachable"
  const isFeedDown = query.isError;

  const tooltipText = isFeedDown
    ? "Alerts feed unreachable — last loaded data may be stale"
    : hasAlerts
      ? `${count} ${count === 1 ? "alert" : "alerts"} in the last 24 hours`
      : "No alerts in the last 24 hours";

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          asChild
          variant="ghost"
          size="icon"
          aria-label={tooltipText}
          className="relative"
          data-testid="notifications-bell"
        >
          <Link href="/alerts">
            <Bell
              className={`size-4 ${
                isFeedDown
                  ? "text-amber-400"
                  : hasAlerts
                    ? "text-foreground"
                    : "text-muted-foreground"
              }`}
              aria-hidden="true"
            />
            {isFeedDown && (
              <Badge
                className="absolute -right-1 -top-1 h-4 min-w-4 rounded-full bg-amber-500/90 px-1 text-[10px] font-semibold leading-none text-amber-50 hover:bg-amber-500"
                data-testid="notifications-bell-error"
              >
                !
              </Badge>
            )}
            {!isFeedDown && hasAlerts && (
              <Badge
                className="absolute -right-1 -top-1 h-4 min-w-4 rounded-full bg-red-500/90 px-1 text-[10px] font-semibold leading-none text-red-50 hover:bg-red-500"
                data-testid="notifications-bell-badge"
              >
                {count > 99 ? "99+" : count}
              </Badge>
            )}
          </Link>
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">{tooltipText}</TooltipContent>
    </Tooltip>
  );
}

function countRecentAlerts(alerts: Array<{ created_at: string }>): number {
  const cutoff = Date.now() - TWENTY_FOUR_HOURS_MS;
  let count = 0;
  for (const a of alerts) {
    const t = new Date(a.created_at).getTime();
    if (Number.isFinite(t) && t >= cutoff) count++;
  }
  return count;
}
