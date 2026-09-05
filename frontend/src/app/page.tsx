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

const CTA =
  "rounded-control bg-cta px-6 py-3 font-semibold text-white shadow-soft transition-colors hover:bg-pink-600";

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
            Late train? The refund files itself.
          </h1>
          <p className="mx-auto mt-5 max-w-[60ch] text-lg leading-relaxed text-muted">
            Add your ticket once. AutoTrain checks every journey against National Rail&apos;s
            arrival data, and when your train is 15 minutes late or more it prepares your Delay
            Repay claim. The operator pays you directly.
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
            Type it in, or forward your ticket email once that is set up. Either way, once.
          </Step>
          <Step n="2" title="We watch the train">
            Actual arrival times from National Rail, checked after every journey you add.
          </Step>
          <Step n="3" title="Your claim is prepared">
            Filed for you where the operator allows it; one tap where it does not. You are
            paid by the operator, never through us.
          </Step>
        </section>

        <section className="mt-10 grid gap-4 sm:grid-cols-3">
          <Chip title="No forms" tone="brand">
            You do not fill in anything after the ticket. The 28-day window is our problem.
          </Chip>
          <Chip title="Checked before filing" tone="cta">
            Every claim is verified against official arrival data before it goes anywhere.
          </Chip>
          <Chip title="Never holds your money" tone="brand">
            Refunds go from the operator to you. AutoTrain only does the paperwork.
          </Chip>
        </section>

        <p className="mt-16 text-center text-sm text-muted">
          Delay Repay is a legal right on UK rail. Most people never claim it.
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
