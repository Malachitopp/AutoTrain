"use client";

/**
 * The operators AutoTrain can file with, from GET /operators.
 *
 * Shown on the landing page before anyone signs in, and again (compact) under
 * the add-journey form. An operator is on the list when the backend has a
 * verified link to its Delay Repay form. A journey on any other operator is
 * tracked but never claimed (the claims sweep skips it), so people must be
 * told which operators count before they bother.
 *
 * Fetched in the browser, like everything else in this app. The landing page
 * is otherwise static, and tying its build to a running API for a list that
 * changes a few times a year would be the wrong trade. If the request fails
 * the section says so and the rest of the page stands.
 */

import { useEffect, useState } from "react";

import { operators, type Operator } from "@/lib/api";

type State = { status: "loading" } | { status: "ready"; list: Operator[] } | { status: "failed" };

function useOperators(): State {
  const [state, setState] = useState<State>({ status: "loading" });
  useEffect(() => {
    // StrictMode runs effects twice in development; the flag lets exactly
    // one answer through.
    let cancelled = false;
    operators
      .list()
      .then((list) => {
        if (!cancelled) setState({ status: "ready", list });
      })
      .catch(() => {
        if (!cancelled) setState({ status: "failed" });
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return state;
}

export function SupportedOperators({ compact = false }: { compact?: boolean }) {
  const state = useOperators();
  if (compact) return <Sentence state={state} />;

  return (
    <section
      aria-labelledby="operators-heading"
      className="rounded-card border border-line bg-white/90 p-5 shadow-card backdrop-blur"
    >
      <h2 id="operators-heading" className="text-lg font-bold">
        Works with these operators
      </h2>
      {state.status === "loading" && <p className="mt-2 text-sm text-muted">Loading the list…</p>}
      {state.status === "failed" && (
        <p className="mt-2 text-sm text-muted">The list could not be loaded just now.</p>
      )}
      {state.status === "ready" && (
        <>
          <ul className="mt-3 flex flex-wrap gap-2">
            {state.list.map((operator) => (
              <li
                key={operator.atoc_code}
                className="rounded-full bg-blue-50 px-3 py-1 text-sm font-semibold text-blue-800"
              >
                {operator.name}
              </li>
            ))}
          </ul>
          <p className="mt-3 text-sm leading-relaxed text-muted">{thresholds(state.list)}</p>
        </>
      )}
      <p className="mt-2 text-sm leading-relaxed text-muted">
        If your operator is not listed, AutoTrain cannot file for you yet. More are added as their
        claim forms are verified.
      </p>
    </section>
  );
}

/** The one-line form for the add-journey page. */
function Sentence({ state }: { state: State }) {
  if (state.status === "loading") {
    return <p className="text-sm text-muted">Loading the supported operators…</p>;
  }
  if (state.status === "failed") {
    return (
      <p className="text-sm text-muted">
        The list of supported operators could not be loaded just now.
      </p>
    );
  }
  const names = state.list.map((operator) => operator.name).join(", ");
  return (
    <p className="text-sm text-muted">
      AutoTrain can claim only for journeys with these operators: {names}.
    </p>
  );
}

/** "Claims start at 15 minutes late, or 30 with LNER." Worked out from the
 * data, so the sentence cannot drift from the operators table. */
function thresholds(list: Operator[]): string {
  const minutes = [...new Set(list.map((operator) => operator.min_delay_minutes))].sort(
    (a, b) => a - b,
  );
  if (minutes.length === 0) return "";
  const [lowest, ...higher] = minutes;
  if (higher.length === 0) return `Claims start at ${lowest} minutes late.`;
  const exceptions = higher.map((threshold) => {
    const names = list
      .filter((operator) => operator.min_delay_minutes === threshold)
      .map((operator) => operator.name);
    return `${threshold} with ${joinNames(names)}`;
  });
  return `Claims start at ${lowest} minutes late, or ${exceptions.join(", or ")}.`;
}

function joinNames(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}
