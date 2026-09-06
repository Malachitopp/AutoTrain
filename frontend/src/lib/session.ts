/**
 * Whether this browser has a session, as far as a page can know.
 *
 * The session itself is an httpOnly cookie the API sets on login
 * (POST /auth/login/verify) and the browser sends by itself on every request
 * (api.ts asks for it with credentials: "include"). Page scripts cannot read
 * that cookie — that is the point: a script injected through an XSS hole
 * cannot lift the session either, which it could when the token sat in
 * localStorage.
 *
 * So this module keeps only a HINT: "signed in on this browser", used to
 * choose between "Sign in" and "Your journeys" on the front door before the
 * API has been asked. The hint is never trusted for anything that matters.
 * The gate (use-user.ts) asks GET /auth/me, and the API alone decides; a
 * wrong hint costs one extra request, never a wrong page.
 */

const KEY = "autotrain.signed_in";

/** The hint. False on the server (no storage there) and in a browser that
 * blocks storage: both simply mean "ask the API". Never throws. */
export function hasSession(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

/** Set after a confirmed sign-in. A browser that refuses storage loses only
 * the hint: the cookie is the session, and the next gated visit asks. */
export function remember(): void {
  try {
    window.localStorage.setItem(KEY, "1");
  } catch {
    // Nothing to keep; see above.
  }
}

/** Cleared on sign-out and whenever the API answers 401. */
export function forget(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // Nothing was stored in a browser that blocks storage.
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
