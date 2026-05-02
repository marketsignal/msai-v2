/**
 * Pure polling-cadence helper for onboard-job status queries.
 *
 * Rules (PRD §8.1 + plan Overrides O-7/O-13):
 * - Terminal statuses ("completed" | "failed" | "completed_with_failures")
 *   return `false` so TanStack Query stops polling.
 * - Non-terminal statuses use exponential backoff: 2s base, doubling on each
 *   consecutive identical-status poll, capped at 30s. A status change resets
 *   the cadence to 2s.
 * - Pre-data (no `status` yet) starts at 2s.
 */

const TERMINAL = ["completed", "failed", "completed_with_failures"] as const;

export function computeRefetchInterval(args: {
  status: string | undefined;
  prevStatus: string | undefined;
  consecutiveSameCount: number;
}): number | false {
  if (!args.status) return 2000;
  if ((TERMINAL as readonly string[]).includes(args.status)) return false;
  if (args.status === args.prevStatus) {
    return Math.min(2000 * Math.pow(2, args.consecutiveSameCount), 30_000);
  }
  return 2000;
}
