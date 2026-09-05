/**
 * The address bar's fragment (the part after '#'), kept in step with the
 * browser.
 *
 * Reading window.location.hash once at render time misses two things: the
 * server render, which has no address bar at all, and a link opened in a
 * tab that is already on this page, which changes the fragment without
 * re-rendering anything. useSyncExternalStore handles both: it hands the
 * server value (null) to the server render and to the hydrating render, and
 * it subscribes to the browser's own change events so a new fragment is a
 * new render.
 *
 * Returns null until the browser render, then the fragment string ('' when
 * there is none). So `hash === null` also answers "am I hydrated yet?".
 */

import { useSyncExternalStore } from "react";

function subscribe(onChange: () => void) {
  window.addEventListener("hashchange", onChange);
  window.addEventListener("popstate", onChange);
  return () => {
    window.removeEventListener("hashchange", onChange);
    window.removeEventListener("popstate", onChange);
  };
}

export function useHash(): string | null {
  return useSyncExternalStore(
    subscribe,
    () => window.location.hash,
    () => null,
  );
}
