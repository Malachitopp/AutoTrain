"use client";

/**
 * The gate a signed-in page stands behind.
 *
 * Wrap a page's content in <RequireSession> and it renders only once the API
 * has confirmed who the visitor is. A signed-out visitor is sent to /login;
 * a visitor the API could not check (it is down, or failing) sees what went
 * wrong and a retry, and keeps their session. The confirmed user is then
 * available to anything inside through useCurrentUser(), so a header can
 * show the email without asking the API again.
 *
 * This is the frontend's counterpart of the backend's UserIdDep: one place
 * decides whether the request may proceed, and pages never re-check.
 */

import { useRouter } from "next/navigation";
import { createContext, useContext, useEffect, type ReactNode } from "react";

import type { User } from "@/lib/api";
import { useUser } from "@/lib/use-user";

const CurrentUser = createContext<User | null>(null);

export function RequireSession({ children }: { children: ReactNode }) {
  const router = useRouter();
  const state = useUser();

  // Navigation is a side effect, so it lives in an effect rather than in
  // the render: rendering must only describe the screen.
  useEffect(() => {
    if (state.status === "signed-out") router.replace("/login");
  }, [state.status, router]);

  if (state.status === "unavailable") {
    return (
      <main className="mx-auto max-w-md px-6 pt-24">
        <section className="rounded-card border border-line bg-white/90 p-8 shadow-soft backdrop-blur">
          <h1 className="text-2xl font-extrabold tracking-[-0.02em]">AutoTrain is unavailable</h1>
          <p className="mt-3 text-muted">
            Your sign-in is kept. The service could not be reached just now.
          </p>
          <p role="alert" className="mt-2 text-sm text-red-700">
            {state.message}
          </p>
          <button
            type="button"
            onClick={state.retry}
            className="mt-6 rounded-control bg-cta px-6 py-3 font-semibold text-white shadow-soft transition-colors hover:bg-pink-700"
          >
            Try again
          </button>
        </section>
      </main>
    );
  }

  if (state.status !== "signed-in") {
    return <p className="p-8 text-muted">Checking your session…</p>;
  }
  return <CurrentUser.Provider value={state.user}>{children}</CurrentUser.Provider>;
}

/** The signed-in user. Only valid inside <RequireSession>, which is the
 * only place that can supply one; anywhere else is a programming error. */
export function useCurrentUser(): User {
  const user = useContext(CurrentUser);
  if (user === null) throw new Error("useCurrentUser() must be used inside <RequireSession>");
  return user;
}
