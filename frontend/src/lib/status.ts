/**
 * Shared status display helpers — colors and labels for job/run statuses.
 */

/** Tailwind classes for status badges (job status, run status, trial status). */
export function statusColor(status: string): string {
  switch (status) {
    case "completed":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "running":
      return "bg-blue-500/15 text-blue-500 hover:bg-blue-500/25";
    case "pending":
      return "bg-amber-500/15 text-amber-500 hover:bg-amber-500/25";
    case "failed":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
    case "cancelled":
      return "bg-gray-500/15 text-gray-500 hover:bg-gray-500/25";
    default:
      return "bg-muted text-muted-foreground hover:bg-muted";
  }
}

/** Human-readable label for research job types. */
export function jobTypeLabel(jobType: string): string {
  switch (jobType) {
    case "parameter_sweep":
      return "Parameter Sweep";
    case "walk_forward":
      return "Walk Forward";
    default:
      return jobType;
  }
}
