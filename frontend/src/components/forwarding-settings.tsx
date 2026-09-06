"use client";

/**
 * The settings page: the address a user forwards ticket emails to, and what
 * became of the ones they sent.
 *
 * Until this existed the whole intake feature was invisible — the backend
 * minted an address on first ask and nothing in the app ever asked. So this
 * page is what turns it on for a person.
 *
 * It teaches the two ways to forward, because they behave differently and
 * the difference is not the user's problem to discover. Forwarding by hand
 * works immediately. A rule in their email provider is the one worth having,
 * and it needs one extra step: the provider emails a code to this address
 * before it will start, and that code appears here (backend statuses, 0016).
 */

import { useState } from "react";

import { AppHeader } from "@/components/app-header";
import { describeError, intake, type InboundEmail } from "@/lib/api";
import { clockTime, londonDate, shortDate } from "@/lib/format";
import { describeInbound, pendingSetup } from "@/lib/inbound-status";
import type { Tone } from "@/lib/journey-status";
import { useForwarding } from "@/lib/use-forwarding";

const BUTTON =
  "rounded-control bg-cta px-5 py-2.5 text-sm font-semibold text-white shadow-soft transition-colors hover:bg-pink-700 disabled:opacity-50";
const QUIET =
  "rounded-control border border-line bg-white px-4 py-2 text-sm font-semibold transition-colors hover:bg-slate-50 disabled:opacity-50";
const CARD = "rounded-card border border-line bg-white/90 p-6 shadow-card backdrop-blur";

export function ForwardingSettings() {
  const state = useForwarding();

  return (
    <>
      <AppHeader />
      <main className="mx-auto w-full max-w-3xl px-6 py-10 sm:px-10">
        <h1 className="text-2xl font-extrabold tracking-[-0.02em]">Forward your tickets</h1>
        <p className="mt-2 max-w-[62ch] text-muted">
          Send a booking confirmation to your AutoTrain address and we read the journey out of
          it, so you never type one in again.
        </p>

        {state.status === "loading" && <p className="mt-8 text-muted">Loading…</p>}

        {state.status === "error" && (
          <section className={`mt-8 ${CARD}`}>
            <h2 className="text-xl font-bold">Could not load your settings</h2>
            <p role="alert" className="mt-2 text-sm text-red-700">
              {state.message}
            </p>
            <button type="button" onClick={state.retry} className={`mt-6 ${BUTTON}`}>
              Try again
            </button>
          </section>
        )}

        {state.status === "ready" && (
          <>
            {state.reloadError !== null && (
              <p
                role="alert"
                className="mt-6 rounded-control border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-800"
              >
                Could not refresh: {state.reloadError}{" "}
                <button type="button" onClick={state.reload} className="font-semibold underline">
                  Try again
                </button>
              </p>
            )}

            {state.data.address === null ? (
              <NotSwitchedOn />
            ) : (
              <AddressCard address={state.data.address} onChanged={state.reload} />
            )}

            <SetupNotice email={pendingSetup(state.data.emails)} />
            <EmailList emails={state.data.emails} />
          </>
        )}
      </main>
    </>
  );
}

/** No inbound mail domain is configured on the API. True of every
 * development machine until one is, so it explains rather than alarms. */
function NotSwitchedOn() {
  return (
    <section className={`mt-8 ${CARD}`}>
      <h2 className="text-xl font-bold">Not switched on yet</h2>
      <p className="mt-2 max-w-[62ch] text-muted">
        Forwarding needs a mail domain, and this AutoTrain does not have one configured. Until
        it does you can still add journeys by hand.
      </p>
    </section>
  );
}

type Rotating = { kind: "idle" } | { kind: "asking" } | { kind: "working" } | { kind: "failed"; error: string };

function AddressCard({ address, onChanged }: { address: string; onChanged: () => void }) {
  const [copied, setCopied] = useState(false);
  const [rotating, setRotating] = useState<Rotating>({ kind: "idle" });

  async function copy() {
    try {
      await navigator.clipboard.writeText(address);
      setCopied(true);
    } catch {
      // Clipboard access can be refused (an insecure origin, a browser
      // setting). The address is on screen and selectable either way, so
      // there is nothing to report — just no confirmation to show.
      setCopied(false);
    }
  }

  async function replace() {
    setRotating({ kind: "working" });
    try {
      await intake.rotate();
      setRotating({ kind: "idle" });
      setCopied(false);
      onChanged();
    } catch (error: unknown) {
      setRotating({ kind: "failed", error: describeError(error) });
    }
  }

  return (
    <section className={`mt-8 ${CARD}`}>
      <h2 className="text-lg font-bold">Your address</h2>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <code className="min-w-0 flex-1 overflow-x-auto rounded-control border border-line bg-slate-50 px-4 py-3 font-mono text-sm">
          {address}
        </code>
        <button type="button" onClick={copy} className={QUIET}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>

      <h3 className="mt-6 text-sm font-bold">Two ways to use it</h3>
      <ol className="mt-2 flex list-decimal flex-col gap-2 pl-5 text-sm leading-relaxed text-muted">
        <li>
          Forward a booking email to it by hand. This works straight away, and it is the quickest
          way to see AutoTrain read a real ticket.
        </li>
        <li>
          Or set up a rule in your email so tickets come here on their own. In Gmail that is
          Settings, then Forwarding, then add this address. Your provider will email a code to
          this address before it starts, and the code appears on this page.
        </li>
      </ol>

      <div className="mt-6 border-t border-line pt-5">
        {rotating.kind === "asking" ? (
          <div>
            <p className="text-sm text-muted">
              Replacing gives you a new address and mail sent to the old one will be thrown away.
              If you set up a forwarding rule, you will need to point it at the new address.
            </p>
            <div className="mt-3 flex gap-3">
              <button type="button" onClick={replace} className={BUTTON}>
                Yes, replace it
              </button>
              <button
                type="button"
                onClick={() => setRotating({ kind: "idle" })}
                className={QUIET}
              >
                Keep it
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setRotating({ kind: "asking" })}
            disabled={rotating.kind === "working"}
            className={QUIET}
          >
            {rotating.kind === "working" ? "Replacing…" : "Replace this address"}
          </button>
        )}
        {rotating.kind === "failed" && (
          <p role="alert" className="mt-3 text-sm text-red-700">
            {rotating.error}
          </p>
        )}
        <p className="mt-3 max-w-[62ch] text-xs text-muted">
          Treat it like a password. Anyone who has it can send us tickets in your name, which is
          why nothing is claimed until you confirm it yourself.
        </p>
      </div>
    </section>
  );
}

/** The code a mail provider sent to switch automatic forwarding on. It is
 * the one thing on this page a person must act on, so it is not a row in a
 * list. */
function SetupNotice({ email }: { email: InboundEmail | null }) {
  if (email === null) return null;
  return (
    <section className="mt-6 rounded-card border border-pink-200 bg-pink-50 p-6 shadow-card">
      <h2 className="text-lg font-bold">Finish setting up forwarding</h2>
      <p className="mt-2 max-w-[62ch] text-sm leading-relaxed text-pink-900">
        {email.status_reason ??
          "Your email provider sent a code to confirm forwarding. Check the message below."}
      </p>
      <p className="mt-2 text-xs text-pink-900/80">Arrived {arrived(email)}.</p>
    </section>
  );
}

function EmailList({ emails }: { emails: InboundEmail[] }) {
  return (
    <section className="mt-10">
      <h2 className="text-lg font-bold">Emails you have forwarded</h2>
      {emails.length === 0 ? (
        <p className="mt-3 text-muted">
          Nothing yet. Forward a booking confirmation and it will appear here with what we made
          of it.
        </p>
      ) : (
        <ul className="mt-3 flex flex-col gap-3">
          {emails.map((email) => (
            <EmailRow key={email.id} email={email} />
          ))}
        </ul>
      )}
    </section>
  );
}

function EmailRow({ email }: { email: InboundEmail }) {
  const view = describeInbound(email);
  return (
    <li className="rounded-card border border-line bg-white/90 p-4 shadow-card backdrop-blur">
      <div className="flex flex-wrap items-start gap-x-5 gap-y-2">
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-semibold text-muted">{arrived(email)}</div>
          <div className="truncate font-bold tracking-[-0.01em]">
            {email.subject || "(no subject)"}
          </div>
          <div className="truncate text-sm text-muted">{email.sender || "unknown sender"}</div>
        </div>
        <Pill tone={view.tone}>{view.label}</Pill>
      </div>
      {email.status_reason !== null && !view.needsAction && (
        <p className="mt-2 text-sm text-muted">{email.status_reason}</p>
      )}
    </li>
  );
}

/** "Fri 5 Sep · 14:30", both halves in London time. The date must come from
 * the same clock as the time: near midnight in summer the UTC date and the
 * London date are different days, so slicing the ISO string would pair
 * yesterday's date with today's time. */
function arrived(email: InboundEmail): string {
  const instant = new Date(email.received_at);
  return `${shortDate(londonDate(instant))} · ${clockTime(email.received_at)}`;
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
    <span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${PILL[tone]}`}>
      {children}
    </span>
  );
}
