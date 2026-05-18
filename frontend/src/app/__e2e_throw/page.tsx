/**
 * Test-only route that triggers Next's `error.tsx` boundary by throwing
 * at render time. Per Plan Revision R21:
 *
 * - When `NEXT_PUBLIC_E2E_AUTH_BYPASS !== "1"` (i.e., production), this
 *   route returns 404 via `notFound()` so the route is invisible to
 *   real users.
 * - When `NEXT_PUBLIC_E2E_AUTH_BYPASS === "1"`, the route throws a
 *   deterministic error which the Playwright UC-12b spec asserts
 *   surfaces `error.tsx` (with the Retry CTA + back-to-dashboard).
 *
 * Server component (no `"use client"`) so the env check runs server-side
 * and prevents the gate from leaking into the client bundle.
 */

import { notFound } from "next/navigation";

export default function E2EThrowPage(): never {
  if (process.env.NEXT_PUBLIC_E2E_AUTH_BYPASS !== "1") {
    notFound();
  }
  throw new Error("E2E render-time test crash (gated route /__e2e_throw)");
}
