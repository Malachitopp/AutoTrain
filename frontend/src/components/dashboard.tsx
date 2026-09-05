"use client";

/**
 * The journeys page: the money box, then every journey with its status.
 * With no journeys yet the list is replaced by the one thing to do next.
 * Layout follows the design's screen 1e (a trip list over a refund ledger)
 * in the Soft Modern skin.
 */

import Link from "next/link";

import { AppHeader } from "@/components/app-header";
import { JourneyList } from "@/components/journey-list";
import { MoneyBox } from "@/components/money-box";
import { useDashboard } from "@/lib/use-dashboard";

const ADD = "rounded-control bg-cta px-5 py-2.5 text-sm font-semibold text-white shadow-soft transition-colors hover:bg-pink-700";

export function Dashboard() {
  const state = useDashboard();

  return (
    <>
      <AppHeader />
      <main className="mx-auto w-full max-w-3xl px-6 py-10 sm:px-10">
        {state.status === "loading" && <p className="text-muted">Loading your journeys…</p>}

        {state.status === "error" && (
          <section className="rounded-card border border-line bg-white/90 p-8 shadow-soft backdrop-blur">
            <h1 className="text-2xl font-extrabold tracking-[-0.02em]">Could not load your journeys</h1>
            <p role="alert" className="mt-2 text-sm text-red-700">
              {state.message}
            </p>
            <button type="button" onClick={state.retry} className={`mt-6 ${ADD}`}>
              Try again
            </button>
          </section>
        )}

        {state.status === "ready" && (
          <>
            <MoneyBox summary={state.data.summary} />

            <div className="mt-10 flex items-center justify-between">
              <h1 className="text-2xl font-extrabold tracking-[-0.02em]">Your journeys</h1>
              {state.data.journeys.length > 0 && (
                <Link href="/journeys/new" className={ADD}>
                  Add a journey
                </Link>
              )}
            </div>

            {state.data.journeys.length === 0 ? (
              <section className="mt-4 rounded-card border border-line bg-white/90 p-8 text-center shadow-soft backdrop-blur">
                <h2 className="text-xl font-bold">No journeys yet</h2>
                <p className="mx-auto mt-2 max-w-[44ch] text-muted">
                  Add the first one and we will watch the train for you. If it runs late enough,
                  the claim appears here with the amount and the deadline.
                </p>
                <Link href="/journeys/new" className={`mt-6 inline-block ${ADD}`}>
                  Add your first journey
                </Link>
              </section>
            ) : (
              <div className="mt-4">
                <JourneyList
                  journeys={state.data.journeys}
                  claims={state.data.claims}
                  onChanged={state.reload}
                />
              </div>
            )}
          </>
        )}
      </main>
    </>
  );
}
