/**
 * "Am I running in the browser yet?"
 *
 * Next.js renders every page twice: once on the server to produce HTML, and
 * once in the browser to take that HTML over (hydration). The server has no
 * window, no localStorage and no address bar, so anything read from those
 * must wait until the browser render — and the first browser render has to
 * match the server's HTML exactly, or React discards it.
 *
 * useSyncExternalStore is React's tool for exactly this: it hands the server
 * value (false) to both the server render and the hydrating render, then
 * re-renders once with the browser value (true). Components read browser
 * state only after this returns true, with no state update in an effect.
 */

import { useSyncExternalStore } from "react";

const subscribe = () => () => {};

export function useHydrated(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => true, // in the browser
    () => false, // on the server, and during hydration
  );
}
