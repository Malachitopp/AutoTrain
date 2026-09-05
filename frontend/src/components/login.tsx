"use client";

/**
 * The login screen, both halves of the magic-link flow:
 *
 * 1. A plain visit shows an email form. Submitting it asks the API to send a
 *    link (POST /auth/login/request). The API answers the same way whether
 *    or not the address has an account, and so does this screen.
 * 2. A visit from the emailed link carries #token=... in the address bar.
 *    The screen shows a Continue button; pressing it exchanges the token for
 *    a session (POST /auth/login/verify), stores it, and lands on the
 *    journeys page. A dead link falls back to the form with an explanation.
 *
 * The exchange waits for a click on purpose. Email security scanners open
 * links, and an attacker can send someone a link for the attacker's own
 * account; if the page exchanged the token the moment it loaded, the first
 * would spend the link before the person saw it and the second would sign
 * the person into the wrong account without their knowledge. A click is the
 * person saying "yes, sign me in here".
 */

import { useRouter } from "next/navigation";
import { useState, type ReactNode, type SubmitEvent } from "react";

import { Brand } from "@/components/brand";
import { ApiError, auth, describeError } from "@/lib/api";
import * as session from "@/lib/session";
import { useHash } from "@/lib/use-hash";

type Phase =
  | { kind: "form"; error?: string }
  | { kind: "sending" }
  | { kind: "sent"; email: string }
  | { kind: "verifying" };

export function Login() {
  const router = useRouter();
  const hash = useHash();
  const [phase, setPhase] = useState<Phase>({ kind: "form" });
  const [email, setEmail] = useState("");

  // null until the browser render: the server has no address bar.
  if (hash === null) {
    return (
      <Frame>
        <p className="text-muted">Loading…</p>
      </Frame>
    );
  }
  const linkToken = session.tokenFromHash(hash);

  async function finishSignIn(token: string) {
    setPhase({ kind: "verifying" });
    try {
      const { access_token } = await auth.verifyLogin(token);
      session.store(access_token);
      scrubLink();
      router.replace("/journeys");
    } catch (error: unknown) {
      if (error instanceof ApiError && error.status === 401) {
        // The API has judged the link dead: nothing to retry.
        scrubLink();
        setPhase({
          kind: "form",
          error: "That link has expired or was already used. Request a new one.",
        });
      } else if (error instanceof session.StorageUnavailable) {
        // The token was spent but the session cannot be kept.
        scrubLink();
        setPhase({ kind: "form", error: error.message });
      } else {
        // The API never judged the token (unreachable, or failing), so the
        // link is still good. It stays in the address bar and Continue
        // becomes a retry.
        setPhase({ kind: "form", error: describeError(error) });
      }
    }
  }

  async function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setPhase({ kind: "sending" });
    try {
      await auth.requestLogin(email);
      setPhase({ kind: "sent", email });
    } catch (error: unknown) {
      setPhase({ kind: "form", error: describeError(error) });
    }
  }

  if (phase.kind === "verifying") {
    return (
      <Frame>
        <p className="text-muted">Signing you in…</p>
      </Frame>
    );
  }

  if (linkToken !== null) {
    return (
      <Frame>
        <h1 className="text-2xl font-extrabold tracking-[-0.02em]">Finish signing in</h1>
        <p className="mt-3 leading-relaxed text-muted">
          You followed a sign-in link. Press continue to open your account on this browser.
        </p>
        {session.token() !== null && (
          <p className="mt-2 text-sm text-muted">
            This browser is already signed in. Continuing switches it to the account the link
            belongs to.
          </p>
        )}
        <button
          type="button"
          onClick={() => finishSignIn(linkToken)}
          className="mt-6 w-full rounded-control bg-cta px-6 py-3 font-semibold text-white shadow-soft transition-colors hover:bg-pink-700"
        >
          Continue
        </button>
        {phase.kind === "form" && phase.error !== undefined && (
          <p role="alert" className="mt-4 text-sm text-red-700">
            {phase.error}
          </p>
        )}
      </Frame>
    );
  }

  if (phase.kind === "sent") {
    return (
      <Frame>
        <h1 className="text-2xl font-extrabold tracking-[-0.02em]">Check your inbox</h1>
        <p className="mt-3 leading-relaxed text-muted">
          If <span className="font-semibold text-ink">{phase.email}</span> can receive mail, a
          sign-in link is on its way. It works once and expires in 15 minutes.
        </p>
      </Frame>
    );
  }

  return (
    <Frame>
      <h1 className="text-2xl font-extrabold tracking-[-0.02em]">Sign in</h1>
      <p className="mt-2 text-muted">No password. We email you a link.</p>
      <form onSubmit={submit} className="mt-6 flex flex-col gap-2">
        <label htmlFor="email" className="text-[11px] font-semibold text-muted">
          Email
        </label>
        <input
          id="email"
          type="email"
          required
          autoComplete="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          className="rounded-control border border-line bg-white px-4 py-3 text-[15px] font-semibold"
        />
        <button
          type="submit"
          disabled={phase.kind === "sending"}
          className="mt-3 rounded-control bg-cta px-6 py-3 font-semibold text-white shadow-soft transition-colors hover:bg-pink-700 disabled:opacity-50"
        >
          {phase.kind === "sending" ? "Sending…" : "Email me a link"}
        </button>
      </form>
      {phase.kind === "form" && phase.error !== undefined && (
        <p role="alert" className="mt-4 text-sm text-red-700">
          {phase.error}
        </p>
      )}
    </Frame>
  );
}

/** The card every state of this screen sits in, under the brand. */
function Frame({ children }: { children: ReactNode }) {
  return (
    <main className="flex flex-1 flex-col items-center px-6 pt-24 pb-16">
      <div className="mb-8">
        <Brand />
      </div>
      <section className="w-full max-w-md rounded-card border border-line bg-white/90 p-8 shadow-soft backdrop-blur">
        {children}
      </section>
    </main>
  );
}

/** Remove the token from the address bar and from history, so a reload, a
 * bookmark or a shared screenshot cannot replay it. */
function scrubLink() {
  window.history.replaceState(null, "", window.location.pathname);
}
