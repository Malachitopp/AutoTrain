import Link from "next/link";

import { Brand } from "@/components/brand";
import { SignInLink } from "@/components/sign-in-link";

// The front door. Public, static, and short: what AutoTrain does in one
// line, how it works in three, and one button. Everything behind the button
// lives under /journeys and needs a session; nothing here does.
//
// A server component: no state, no browser access, so it renders once on
// the server and ships no JavaScript of its own. The one part that depends
// on the browser (which button to show) is the SignInLink client component.
//
// Every sentence here describes what the backend does today (deep-link
// filing, per-operator thresholds, no money handling). When server-side
// filing lands (claims adapters v2), the third step and the first chip are
// the lines to update.

const CTA =
  "rounded-control bg-cta px-6 py-3 font-semibold text-white shadow-soft transition-colors hover:bg-pink-700";

export default function LandingPage() {
  return (
    <>
      <header className="flex items-center gap-7 border-b border-line/70 bg-white/80 px-6 py-4 backdrop-blur sm:px-10">
        <Link href="/" aria-label="AutoTrain home">
          <Brand />
        </Link>
        <a href="#how" className="ml-3.5 hidden text-sm font-semibold text-muted hover:text-ink sm:inline">
          How it works
        </a>
        <SignInLink className={`ml-auto ${CTA} px-5 py-2.5 text-sm`} />
      </header>

      <main className="mx-auto w-full max-w-3xl px-6 pt-20 pb-24 sm:px-10">
        <section className="text-center">
          <h1 className="text-5xl font-extrabold leading-[1.05] tracking-[-0.03em] sm:text-6xl">
            Late train? Your refund claim, worked out for you.
          </h1>
          <p className="mx-auto mt-5 max-w-[60ch] text-lg leading-relaxed text-muted">
            Add your journey once. AutoTrain checks it against National Rail&apos;s arrival data.
            If the train was late enough for your operator&apos;s Delay Repay scheme, we work out
            the amount and open a claim with its deadline. The operator pays you directly.
          </p>
          <div className="mt-8 flex items-center justify-center gap-4">
            <SignInLink className={`${CTA} text-base`} />
            <a href="#how" className="text-sm font-semibold text-muted hover:text-ink">
              How it works
            </a>
          </div>
        </section>

        <section id="how" className="mt-20 grid gap-4 sm:grid-cols-3">
          <Step n="1" title="Add a journey">
            Where from, where to, the date, the operator and the ticket price. Once.
          </Step>
          <Step n="2" title="We watch the train">
            Actual arrival times from National Rail, checked after every journey you add. Most
            operators pay from 15 minutes late; a few, such as LNER and ScotRail, from 30.
          </Step>
          <Step n="3" title="Your claim is ready">
            We open it with the amount and the deadline. Tap File and we take you to your
            operator&apos;s Delay Repay page to send it. Sending it for you is coming, with your
            permission.
          </Step>
        </section>

        <section className="mt-10 grid gap-4 sm:grid-cols-3">
          <Chip title="We do the working out" tone="brand">
            How late the train was, how much you are owed, and the date to claim by. You send
            the form; we show you the deadline.
          </Chip>
          <Chip title="Checked before filing" tone="cta">
            Every claim is checked against official arrival data before it goes anywhere.
          </Chip>
          <Chip title="Never holds your money" tone="brand">
            Refunds go from the operator to you. AutoTrain never takes or holds any of it.
          </Chip>
        </section>

        <p className="mt-16 text-center text-sm text-muted">
          Most UK train operators run a Delay Repay scheme. Most passengers never claim.
        </p>
      </main>
    </>
  );
}

function Step({ n, title, children }: { n: string; title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-card border border-line bg-white/90 p-5 shadow-card backdrop-blur">
      <div className="text-[11px] font-semibold tracking-wide text-brand">STEP {n}</div>
      <h2 className="mt-1 text-lg font-bold">{title}</h2>
      <p className="mt-2 text-sm leading-relaxed text-muted">{children}</p>
    </div>
  );
}

function Chip({
  title,
  tone,
  children,
}: {
  title: string;
  tone: "brand" | "cta";
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-card border border-line bg-white/90 p-4 shadow-card backdrop-blur">
      <div className="flex items-center gap-2.5">
        <span
          className={`grid h-5 w-5 flex-none place-items-center rounded-full ${
            tone === "brand" ? "bg-brand" : "bg-cta"
          }`}
        >
          <svg
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            stroke="white"
            strokeWidth="3"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M20 6 9 17l-5-5" />
          </svg>
        </span>
        <span className="text-[15px] font-semibold">{title}</span>
      </div>
      <p className="mt-1.5 text-[13px] leading-relaxed text-muted">{children}</p>
    </div>
  );
}
