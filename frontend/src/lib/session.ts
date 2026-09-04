/**
 * Where the session token lives in the browser.
 *
 * The API issues a bearer JWT (POST /auth/login/verify) and expects it back on
 * every request as `Authorization: Bearer <jwt>`. This module is the only
 * place that knows the token is kept in localStorage: the api module asks for
 * it, the login page stores it, the sign-out button clears it, and nothing
 * else touches storage. Swapping the storage later means editing one file.
 *
 * localStorage is readable by any script running on the page, so an XSS hole
 * would leak sessions. The production hardening is to keep the token in an
 * httpOnly cookie set by a Next.js route handler; the three functions below
 * stay the same, only their bodies change.
 */

const KEY = "autotrain.session";

/** The stored session token, or null when signed out (or when rendering on
 * the server, where there is no browser storage). Never throws: a browser
 * that blocks storage reads as signed out. */
export function token(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function store(jwt: string): void {
  window.localStorage.setItem(KEY, jwt);
}

export function clear(): void {
  window.localStorage.removeItem(KEY);
}
