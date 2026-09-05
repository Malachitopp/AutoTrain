/**
 * Everything the journeys page shows, loaded in one go.
 *
 * Three requests run together: the money summary, the journeys, and the
 * claims. Claims are matched to journeys in the browser by journey_id, so
 * no request depends on another and the page appears as soon as the slowest
 * one answers. `reload` runs all three again — after filing a claim, say.
 *
 * Once the page has good data it never blanks out: a reload keeps the last
 * good copy on screen while the new one is fetched, and a reload that fails
 * keeps it too, with the failure reported alongside. Only a page that has
 * never loaded shows the full error screen.
 */

import { useEffect, useState } from "react";

import { claims, describeError, journeys, type Claim, type ClaimSummary, type Journey } from "@/lib/api";

export type Dashboard = { summary: ClaimSummary; journeys: Journey[]; claims: Claim[] };

export type DashboardState =
  | { status: "loading" }
  | { status: "error"; message: string; retry: () => void }
  | {
      status: "ready";
      data: Dashboard;
      reload: () => void;
      /** A reload is in flight; `data` is the last good copy. */
      reloading: boolean;
      /** The last reload failed; `data` is still the last good copy. */
      reloadError: string | null;
    };

/** Each answer remembers which attempt it answers: an answer to an earlier
 * attempt is stale once a newer one has started. */
type Good = { attempt: number; data: Dashboard };
type Failed = { attempt: number; message: string };

export function useDashboard(): DashboardState {
  const [attempt, setAttempt] = useState(0);
  const [good, setGood] = useState<Good | null>(null);
  const [failed, setFailed] = useState<Failed | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([claims.summary(), journeys.list(), claims.list()])
      .then(([summary, journeyPage, claimPage]) => {
        if (cancelled) return;
        setGood({ attempt, data: { summary, journeys: journeyPage.items, claims: claimPage.items } });
      })
      .catch((error: unknown) => {
        if (!cancelled) setFailed({ attempt, message: describeError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const again = () => setAttempt((n) => n + 1);
  const failedNow = failed !== null && failed.attempt === attempt ? failed.message : null;

  if (good === null) {
    // Nothing has ever loaded: a failure is the whole screen.
    return failedNow !== null
      ? { status: "error", message: failedNow, retry: again }
      : { status: "loading" };
  }
  return {
    status: "ready",
    data: good.data,
    reload: again,
    reloading: good.attempt !== attempt && failedNow === null,
    reloadError: failedNow,
  };
}
