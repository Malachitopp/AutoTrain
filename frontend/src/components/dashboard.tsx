"use client";

/**
 * The home screen's frame: the header from the design (screen 4a) with who
 * is signed in and how to sign out. The journey card and the refund ledger
 * (screen 1e) arrive in the next step and slot in below.
 */

import { useRouter } from "next/navigation";

import { Brand } from "@/components/brand";
import { useCurrentUser } from "@/components/require-session";
import * as session from "@/lib/session";

export function Dashboard() {
  const user = useCurrentUser();
  const router = useRouter();

  function signOut() {
    // Forget the token here; the API keeps no session state to tell.
    session.clear();
    router.replace("/login");
  }

  return (
    <>
      <header className="flex items-center gap-7 border-b border-line/70 bg-white/80 px-10 py-4 backdrop-blur">
        <Brand />
        <span className="ml-3.5 text-sm font-semibold">Journeys</span>
        <span className="ml-auto text-sm text-muted">{user.email}</span>
        <button
          type="button"
          onClick={signOut}
          className="rounded-control border border-line bg-white px-4 py-2 text-sm font-semibold transition-colors hover:bg-slate-50"
        >
          Sign out
        </button>
      </header>
      <main className="mx-auto w-full max-w-4xl px-10 py-16">
        <h1 className="text-4xl font-extrabold tracking-[-0.03em]">Your journeys</h1>
        <p className="mt-3 max-w-[60ch] text-lg leading-relaxed text-muted">
          Add a journey, and when the train runs late the claim files itself. Your journeys and
          claims will appear here.
        </p>
      </main>
    </>
  );
}
