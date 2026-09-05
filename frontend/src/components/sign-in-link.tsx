"use client";

/**
 * The landing page's one button. "Sign in" for a visitor; "Your journeys"
 * for someone this browser already knows, so a returning user is one press
 * from the app and never sees a login form they do not need.
 *
 * The server cannot see storage, so it renders "Sign in"; the browser
 * agrees on its first render (useHydrated is false) and flips afterwards
 * if a token exists. That order is what keeps the server HTML valid.
 */

import Link from "next/link";

import * as session from "@/lib/session";
import { useHydrated } from "@/lib/use-hydrated";

export function SignInLink({ className }: { className?: string }) {
  const hydrated = useHydrated();
  const signedIn = hydrated && session.token() !== null;
  return signedIn ? (
    <Link href="/journeys" className={className}>
      Your journeys
    </Link>
  ) : (
    <Link href="/login" className={className}>
      Sign in
    </Link>
  );
}
