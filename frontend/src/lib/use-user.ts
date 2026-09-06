/**
 * "Who am I?" — the question every signed-in page asks first.
 *
 * The session is a cookie the page cannot read (session.ts), so the only
 * way to know is to ask: GET /auth/me, once per visit, with the browser
 * sending the cookie. The API is the only judge — of whether a session
 * exists at all, whether it has expired, whether the user signed out
 * everywhere since, whether the account was erased. This hook asks and
 * reports one of four answers. Pages render on the answer; the gate in
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
  /** The API could not answer (unreachable, or failing). Nothing has said
   * the session is bad, so nothing is forgotten. */
  | { status: "unavailable"; message: string; retry: () => void }
  | { status: "signed-in"; user: User };

/** What the API said, and which attempt it was answering: an answer to an
 * earlier attempt is stale once a retry has started. */
type Answer =
  | { attempt: number; kind: "user"; user: User }
  | { attempt: number; kind: "refused" }
  | { attempt: number; kind: "unavailable"; message: string };

export function useUser(): UserState {
  const hydrated = useHydrated();
  const [attempt, setAttempt] = useState(0);
  const [answer, setAnswer] = useState<Answer | null>(null);

  useEffect(() => {
    // The server has no cookie jar of its own to ask with; the question is
    // the browser's, so it waits for the browser render.
    if (!hydrated) return;
    // React runs effects twice in development (StrictMode) to surface
    // missing cleanups. The flag makes the first run's answer harmless, so
    // exactly one answer wins.
    let cancelled = false;
    auth
      .me()
      .then((user) => {
        if (cancelled) return;
        session.remember();
        setAnswer({ attempt, kind: "user", user });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 401) {
          // No session, or one the API no longer accepts. The hint on the
          // front door must not keep promising an app that will bounce.
          session.forget();
          setAnswer({ attempt, kind: "refused" });
          return;
        }
        // Anything else (API down, 5xx) has not judged the session, so the
        // page shows the problem and offers a retry.
        setAnswer({ attempt, kind: "unavailable", message: describeError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [hydrated, attempt]);

  if (!hydrated) return { status: "checking" };
  // An answer to an earlier attempt is stale: a fresh check is under way.
  if (answer === null || answer.attempt !== attempt) return { status: "checking" };
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
