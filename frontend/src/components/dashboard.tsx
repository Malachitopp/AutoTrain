"use client";

/**
 * The home screen's frame: who is signed in and how to sign out. The money
 * box and the journey list arrive in the next step and slot in below.
 */

import { useRouter } from "next/navigation";

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
    <main className="mx-auto max-w-2xl p-8">
      <header className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">AutoTrain</h1>
        <div className="flex items-center gap-4 text-sm">
          <span className="text-zinc-600">{user.email}</span>
          <button
            type="button"
            onClick={signOut}
            className="rounded border border-zinc-300 px-3 py-1 hover:bg-zinc-100"
          >
            Sign out
          </button>
        </div>
      </header>
      <p className="mt-8 text-zinc-600">Your journeys and claims will appear here.</p>
    </main>
  );
}
