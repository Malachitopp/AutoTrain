"use client";

/**
 * The gate a signed-in page stands behind.
 *
 * Wrap a page's content in <RequireSession> and it renders only once the API
 * has confirmed who the visitor is; a signed-out visitor is sent to /login
 * instead. The confirmed user is then available to anything inside through
 * useCurrentUser(), so a header can show the email without asking the API
 * again.
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

  if (state.status !== "signed-in") {
    return <p className="p-8 text-zinc-500">Checking your session…</p>;
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
