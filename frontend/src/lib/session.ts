/**
 * The session's lifecycle in the browser: it begins with a login link and
 * then lives in storage.
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

/** Thrown by store() when the browser refuses to keep anything: site data
 * blocked, private mode in some browsers, storage full. The message is
 * written for the person, not the developer. */
export class StorageUnavailable extends Error {
  constructor() {
    super(
      "Your browser is blocking storage for this site, so you cannot stay signed in. " +
        "Allow site data for AutoTrain and request a new link.",
    );
    this.name = "StorageUnavailable";
  }
}

export function store(jwt: string): void {
  try {
    window.localStorage.setItem(KEY, jwt);
  } catch {
    throw new StorageUnavailable();
  }
}

export function clear(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // Nothing was ever stored in a browser that blocks storage.
  }
}

/** The login token carried by an emailed link, or null if the address bar
 * holds none.
 *
 * The link is <app base url>/login#token=<token> — a contract with the
 * backend's identity.request_login. The token rides in the fragment (after
 * the '#') because a browser never sends that part to any server, so it
 * stays out of request logs; only this code, running in the page, sees it.
 * Pass `window.location.hash`, which includes the leading '#'. */
export function tokenFromHash(hash: string): string | null {
  const token = new URLSearchParams(hash.replace(/^#/, "")).get("token");
  return token ? token : null;
}
