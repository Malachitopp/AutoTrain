/**
 * The first thing on the journeys page: how much has come back, and how
 * much is in claims not yet paid. From GET /claims/summary.
 */

import type { ClaimSummary } from "@/lib/api";
import { pounds } from "@/lib/format";

export function MoneyBox({ summary }: { summary: ClaimSummary }) {
  return (
    <section
      aria-labelledby="money-heading"
      className="rounded-card border border-line bg-white/90 p-6 shadow-soft backdrop-blur"
    >
      <h2 id="money-heading" className="text-[11px] font-semibold tracking-wide text-brand">
        MONEY BACK
      </h2>
      <div className="mt-2 flex flex-wrap items-baseline gap-x-10 gap-y-3">
        <div>
          <div className="text-4xl font-extrabold tracking-[-0.03em]">
            {pounds(summary.recovered_pence)}
          </div>
          <div className="mt-1 text-sm text-muted">paid back to you</div>
        </div>
        <div>
          <div className="text-2xl font-bold tracking-[-0.02em]">{pounds(summary.pending_pence)}</div>
          <div className="mt-1 text-sm text-muted">in claims not yet paid</div>
        </div>
      </div>
    </section>
  );
}
