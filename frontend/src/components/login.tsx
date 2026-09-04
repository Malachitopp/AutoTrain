"use client";

/**
 * The login screen, both halves of the magic-link flow:
 *
 * 1. A plain visit shows an email form. Submitting it asks the API to send a
 *    link (POST /auth/login/request). The API answers the same way whether
 *    or not the address has an account, and so does this screen.
 * 2. A visit from the emailed link carries #token=... in the address bar.
 *    The token is exchanged for a session (POST /auth/login/verify), stored,
 *    and the user lands on the home page. A dead link falls back to the form
 *    with an explanation.
 */

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError, auth, describeError } from "@/lib/api";
import * as session from "@/lib/session";
import { useHydrated } from "@/lib/use-hydrated";

type Phase = { kind: "form"; error?: string } | { kind: "sending" } | { kind: "sent"; email: string };

export function Login() {
  const router = useRouter();
  const hydrated = useHydrated();
  const [phase, setPhase] = useState<Phase>({ kind: "form" });
  const [email, setEmail] = useState("");
  // The token this screen has already started exchanging. A ref, not state:
  // it exists so the effect below runs the exchange once, even though React
  // re-runs effects in development (StrictMode).
  const exchanging = useRef<string | null>(null);

  // The address bar exists only in the browser, so it is read after
  // hydration. While the link's token is still in it and nothing has gone
  // wrong, the exchange is under way.
  const linkToken = hydrated ? session.tokenFromHash(window.location.hash) : null;
  const verifying = linkToken !== null && phase.kind === "form" && phase.error === undefined;

  useEffect(() => {
    if (linkToken === null || exchanging.current === linkToken) return;
    exchanging.current = linkToken;
    auth
      .verifyLogin(linkToken)
      .then(({ access_token }) => {
        scrubLink();
        session.store(access_token);
        router.replace("/");
      })
      .catch((error: unknown) => {
        scrubLink();
        const message =
          error instanceof ApiError && error.status === 401
            ? "That link has expired or was already used. Request a new one."
            : describeError(error);
        setPhase({ kind: "form", error: message });
      });
  }, [linkToken, router]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPhase({ kind: "sending" });
    try {
      await auth.requestLogin(email);
      setPhase({ kind: "sent", email });
    } catch (error: unknown) {
      setPhase({ kind: "form", error: describeError(error) });
    }
  }

  if (!hydrated || verifying) {
    return <p className="p-8 text-zinc-500">Signing you in…</p>;
  }

  if (phase.kind === "sent") {
    return (
      <section className="mx-auto max-w-sm p-8">
        <h1 className="text-2xl font-semibold">Check your inbox</h1>
        <p className="mt-4 text-zinc-600">
          If <span className="font-medium">{phase.email}</span> can receive mail, a sign-in link
          is on its way. It works once and expires in 15 minutes.
        </p>
      </section>
    );
  }

  return (
    <section className="mx-auto max-w-sm p-8">
      <h1 className="text-2xl font-semibold">Sign in to AutoTrain</h1>
      <p className="mt-2 text-zinc-600">No password. We email you a link.</p>
      <form onSubmit={submit} className="mt-6 flex flex-col gap-3">
        <label htmlFor="email" className="text-sm font-medium">
          Email
        </label>
        <input
          id="email"
          type="email"
          required
          autoComplete="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          className="rounded border border-zinc-300 px-3 py-2"
        />
        <button
          type="submit"
          disabled={phase.kind === "sending"}
          className="rounded bg-zinc-900 px-4 py-2 font-medium text-white disabled:opacity-50"
        >
          {phase.kind === "sending" ? "Sending…" : "Email me a link"}
        </button>
      </form>
      {phase.kind === "form" && phase.error !== undefined && (
        <p role="alert" className="mt-4 text-sm text-red-700">
          {phase.error}
        </p>
      )}
    </section>
  );
}

/** Remove the token from the address bar and from history, so a reload, a
 * bookmark or a shared screenshot cannot replay it. */
function scrubLink() {
  window.history.replaceState(null, "", window.location.pathname);
}
