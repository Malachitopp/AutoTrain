/**
 * Everything the journeys page shows, loaded in one go.
 *
 * Three requests run together: the money summary, the journeys, and the
 * claims. Claims are matched to journeys in the browser by journey_id, so
 * no request depends on another and the page appears as soon as the slowest
 * one answers. `reload` runs all three again — after filing a claim, say.
 */

import { useEffect, useState } from "react";

import { claims, describeError, journeys, type Claim, type ClaimSummary, type Journey } from "@/lib/api";

export type Dashboard = { summary: ClaimSummary; journeys: Journey[]; claims: Claim[] };

export type DashboardState =
  | { status: "loading" }
  | { status: "error"; message: string; retry: () => void }
  /** `reloading` is true while a reload is in flight: the data shown is the
   * last good copy, so the page never blanks out under the person. */
  | { status: "ready"; data: Dashboard; reload: () => void; reloading: boolean };

/** What came back, and for which attempt: an answer to an earlier attempt
 * is stale once a reload has started. */
type Result = { attempt: number; data: Dashboard } | { attempt: number; error: string };

export function useDashboard(): DashboardState {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([claims.summary(), journeys.list(), claims.list()])
      .then(([summary, journeyPage, claimPage]) => {
        if (cancelled) return;
        setResult({
          attempt,
          data: { summary, journeys: journeyPage.items, claims: claimPage.items },
        });
      })
      .catch((error: unknown) => {
        if (!cancelled) setResult({ attempt, error: describeError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const again = () => setAttempt((n) => n + 1);

  if (result === null) return { status: "loading" };
  if ("error" in result) {
    // A retry after an error has nothing to show meanwhile.
    if (result.attempt !== attempt) return { status: "loading" };
    return { status: "error", message: result.error, retry: again };
  }
  // A reload after good data keeps showing that data until the new copy
  // lands; rows keep their own state (an opened claim link, say) because
  // they are never unmounted.
  return { status: "ready", data: result.data, reload: again, reloading: result.attempt !== attempt };
}
