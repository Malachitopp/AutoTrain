"use client";

/**
 * The header on every signed-in page: brand, who is signed in, sign out.
 * Pulled out of the dashboard the moment a second signed-in page (the
 * add-journey form) needed it.
 */

import Link from "next/link";
import { useRouter } from "next/navigation";

import { Brand } from "@/components/brand";
import { useCurrentUser } from "@/components/require-session";
import { auth } from "@/lib/api";
import * as session from "@/lib/session";

export function AppHeader() {
  const user = useCurrentUser();
  const router = useRouter();

  async function signOut() {
    // The cookie is httpOnly, so only the API can drop it. If that call
    // fails (API down) the hint is cleared anyway and the front door shows
    // "Sign in"; the next gated visit asks the API and the cookie, if it
    // survived, simply signs the person back in. Then the front door, not
    // the login form: signing out is not a request to sign in again.
    try {
      await auth.logout();
    } catch {
      // See above.
    }
    session.forget();
    router.replace("/");
  }

  return (
    <header className="flex items-center gap-7 border-b border-line/70 bg-white/80 px-6 py-4 backdrop-blur sm:px-10">
      <Link href="/journeys" aria-label="Your journeys">
        <Brand />
      </Link>
      <Link
        href="/settings"
        className="ml-auto text-sm font-semibold text-muted transition-colors hover:text-ink"
      >
        Settings
      </Link>
      <span className="hidden truncate text-sm text-muted sm:inline">{user.email}</span>
      <button
        type="button"
        onClick={signOut}
        className="rounded-control border border-line bg-white px-4 py-2 text-sm font-semibold transition-colors hover:bg-slate-50"
      >
        Sign out
      </button>
    </header>
  );
}
