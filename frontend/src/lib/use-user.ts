/**
 * "Who am I?" — the question every signed-in page asks first.
 *
 * A stored token proves nothing by itself: it may have expired, or the
 * account may have been erased since it was issued. The API is the only
 * judge (GET /auth/me), so this hook asks it once and reports one of three
 * answers. Pages render on the answer; the gate in
 * components/require-session.tsx turns "signed-out" into a redirect.
 */

import { useEffect, useState } from "react";

import { ApiError, auth, type User } from "@/lib/api";
import * as session from "@/lib/session";
import { useHydrated } from "@/lib/use-hydrated";

export type UserState =
  | { status: "checking" }
  | { status: "signed-out" }
  | { status: "signed-in"; user: User };

/** What the API said, once it has said anything. */
type Answer = { kind: "user"; user: User } | { kind: "refused" };

export function useUser(): UserState {
  const hydrated = useHydrated();
  // Read during render, not in an effect: storage is synchronous, and the
  // value decides what to render right now.
  const stored = hydrated ? session.token() : null;
  const [answer, setAnswer] = useState<Answer | null>(null);

  useEffect(() => {
    if (stored === null) return;
    // React runs effects twice in development (StrictMode) to surface
    // missing cleanups. The flag makes the first run's answer harmless, so
    // exactly one answer wins.
    let cancelled = false;
    auth
      .me()
      .then((user) => {
        if (!cancelled) setAnswer({ kind: "user", user });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        // 401: the token is dead (expired, or the account is gone). Forget
        // it, so the next visit goes straight to the login form instead
        // of asking again. Any other failure (API down) keeps the token:
        // it may still be good once the API is back.
        if (error instanceof ApiError && error.status === 401) session.clear();
        setAnswer({ kind: "refused" });
      });
    return () => {
      cancelled = true;
    };
  }, [stored]);

  if (!hydrated) return { status: "checking" };
  if (stored === null) return { status: "signed-out" };
  if (answer === null) return { status: "checking" };
  if (answer.kind === "user") return { status: "signed-in", user: answer.user };
  return { status: "signed-out" };
}
