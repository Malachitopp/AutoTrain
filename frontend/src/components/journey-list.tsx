"use client";

/**
 * The journey rows. Each row is one journey with its status line, worked
 * out by lib/journey-status from the journey and its claim (if any). Rows
 * whose claim is waiting on the user carry a File button: it asks the API
 * for the operator's claim page (POST /claims/{id}/file) and hands the
 * person the link.
 */

import { useState } from "react";

import { claims, describeError, type Claim, type Journey } from "@/lib/api";
import { clockTime, pounds, shortDate } from "@/lib/format";
import { describeJourney, type Tone } from "@/lib/journey-status";

export function JourneyList({
  journeys,
  claims: claimRows,
  onChanged,
}: {
  journeys: Journey[];
  claims: Claim[];
  onChanged: () => void;
}) {
  const claimByJourney = new Map(claimRows.map((claim) => [claim.journey_id, claim]));
  const now = new Date();
  return (
    <ul className="flex flex-col gap-3">
      {journeys.map((journey) => (
        <JourneyRow
          key={journey.id}
          journey={journey}
          claim={claimByJourney.get(journey.id)}
          now={now}
          onChanged={onChanged}
        />
      ))}
    </ul>
  );
}

type Filing = { kind: "idle" } | { kind: "opening" } | { kind: "opened"; url: string } | { kind: "failed"; error: string };

function JourneyRow({
  journey,
  claim,
  now,
  onChanged,
}: {
  journey: Journey;
  claim: Claim | undefined;
  now: Date;
  onChanged: () => void;
}) {
  const view = describeJourney(journey, claim, now);
  const [filing, setFiling] = useState<Filing>({ kind: "idle" });

  async function file() {
    if (claim === undefined) return;
    setFiling({ kind: "opening" });
    try {
      const { url } = await claims.file(claim.id);
      // The link is shown as well as opened: browsers may block a window
      // opened after a network wait, and the person still needs the page.
      window.open(url, "_blank", "noopener");
      setFiling({ kind: "opened", url });
      onChanged();
    } catch (error: unknown) {
      setFiling({ kind: "failed", error: describeError(error) });
    }
  }

  return (
    <li className="rounded-card border border-line bg-white/90 p-4 shadow-card backdrop-blur">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-semibold text-muted">{shortDate(journey.travel_date)}</div>
          <div className="text-lg font-bold tracking-[-0.01em]">
            {journey.origin_crs} → {journey.destination_crs}
          </div>
          <div className="text-sm text-muted">
            {clockTime(journey.scheduled_departure)} → {clockTime(journey.scheduled_arrival)}
          </div>
        </div>
        {view.amountPence !== null && (
          <div className="text-xl font-extrabold tracking-[-0.02em]">{pounds(view.amountPence)}</div>
        )}
        <div className="flex flex-col items-end gap-1">
          <Pill tone={view.tone}>{view.label}</Pill>
          {view.fileBy !== null && (
            <span className="text-[11px] text-muted">by {shortDate(view.fileBy)}</span>
          )}
        </div>
        {view.canFile && (
          <button
            type="button"
            onClick={file}
            disabled={filing.kind === "opening"}
            className="rounded-control bg-cta px-4 py-2 text-sm font-semibold text-white shadow-soft transition-colors hover:bg-pink-700 disabled:opacity-50"
          >
            {filing.kind === "opening" ? "Opening…" : "File"}
          </button>
        )}
      </div>
      {filing.kind === "opened" && (
        <p className="mt-3 text-sm text-muted">
          The operator&apos;s claim page is open in a new tab. If it did not appear,{" "}
          <a href={filing.url} target="_blank" rel="noopener noreferrer" className="font-semibold text-brand">
            open it here
          </a>
          .
        </p>
      )}
      {filing.kind === "failed" && (
        <p role="alert" className="mt-3 text-sm text-red-700">
          {filing.error}
        </p>
      )}
    </li>
  );
}

const PILL: Record<Tone, string> = {
  muted: "bg-slate-100 text-slate-700",
  brand: "bg-blue-50 text-blue-800",
  cta: "bg-pink-50 text-pink-800",
  good: "bg-green-50 text-green-800",
  bad: "bg-red-50 text-red-800",
};

function Pill({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return (
    <span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${PILL[tone]}`}>{children}</span>
  );
}
