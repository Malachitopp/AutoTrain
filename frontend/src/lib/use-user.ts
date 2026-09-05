/**
 * "Who am I?" — the question every signed-in page asks first.
 *
 * A stored token proves nothing by itself: it may have expired, or the
 * account may have been erased since it was issued. The API is the only
 * judge (GET /auth/me), so this hook asks it once and reports one of four
 * answers. Pages render on the answer; the gate in
 * components/require-session.tsx turns "signed-out" into a redirect and
 * "unavailable" into a message with a retry.
 */

import { useEffect, useState } from "react";

import { ApiError, auth, describeError, type User } from "@/lib/api";
import * as session from "@/lib/session";
import { useHydrated } from "@/lib/use-hydrated";

export type UserState =
  | { status: "checking" }
  | { status: "signed-out" }
  /** The API could not answer (unreachable, or failing). The token is kept:
   * nothing has said it is bad. */
  | { status: "unavailable"; message: string; retry: () => void }
  | { status: "signed-in"; user: User };

/** What the API said, and which token and attempt it was answering. An
 * answer about a token no longer in storage is stale, not a verdict. */
type Answer =
  | { attempt: number; token: string; kind: "user"; user: User }
  | { attempt: number; token: string; kind: "refused" }
  | { attempt: number; token: string; kind: "unavailable"; message: string };

export function useUser(): UserState {
  const hydrated = useHydrated();
  // Read during render, not in an effect: storage is synchronous, and the
  // value decides what to render right now.
  const stored = hydrated ? session.token() : null;
  const [attempt, setAttempt] = useState(0);
  const [answer, setAnswer] = useState<Answer | null>(null);

  useEffect(() => {
    if (stored === null) return;
    // React runs effects twice in development (StrictMode) to surface
    // missing cleanups. The flag makes the first run's answer harmless, so
    // exactly one answer wins.
    let cancelled = false;
    const token = stored;
    auth
      .me()
      .then((user) => {
        if (!cancelled) setAnswer({ attempt, token, kind: "user", user });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 401) {
          // The token is dead (expired, or the account is gone). Forget it,
          // so the next visit goes straight to the login form — but only if
          // it is still the token in storage: another tab may have signed
          // in again while this request was in flight.
          if (session.token() === token) session.clear();
          setAnswer({ attempt, token, kind: "refused" });
          return;
        }
        // Anything else (API down, 5xx) has not judged the token, so it
        // stays; the page shows the problem and offers a retry.
        setAnswer({ attempt, token, kind: "unavailable", message: describeError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [stored, attempt]);

  if (!hydrated) return { status: "checking" };
  if (stored === null) return { status: "signed-out" };
  // An answer to an earlier attempt, or about a token that has since been
  // replaced, is stale: a fresh check is under way.
  if (answer === null || answer.attempt !== attempt || answer.token !== stored) {
    return { status: "checking" };
  }
  if (answer.kind === "user") return { status: "signed-in", user: answer.user };
  if (answer.kind === "unavailable") {
    return {
      status: "unavailable",
      message: answer.message,
      retry: () => setAttempt((n) => n + 1),
    };
  }
  return { status: "signed-out" };
}
