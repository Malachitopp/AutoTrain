/**
 * Everything the settings page shows about ticket forwarding, loaded in one go.
 *
 * Two requests run together: the user's forwarding address, and what became
 * of the emails they have sent to it. They are independent, so the page
 * appears as soon as the slower one answers.
 *
 * The address has a third answer besides "here it is" and "something broke":
 * the API returns 503 when no inbound mail domain is configured, which is
 * simply the state of every development machine until one is. That is not an
 * error to apologise for, so it gets its own state and the page explains
 * rather than alarms.
 *
 * Same shape as use-dashboard: once the page has good data it never blanks
 * out, a reload keeps the last good copy on screen, and only a page that has
 * never loaded shows the whole-screen error.
 */

import { useCallback, useEffect, useState } from "react";

import { ApiError, describeError, intake, type InboundEmail } from "@/lib/api";

export type Forwarding = {
  /** null when the API has no inbound domain configured yet. */
  address: string | null;
  emails: InboundEmail[];
};

export type ForwardingState =
  | { status: "loading" }
  | { status: "error"; message: string; retry: () => void }
  | {
      status: "ready";
      data: Forwarding;
      reload: () => void;
      /** The last reload failed; `data` is still the last good copy. */
      reloadError: string | null;
    };

type Good = { attempt: number; data: Forwarding };
type Failed = { attempt: number; message: string };

/** The address, or null when the API says no domain is configured. Any
 * other failure is a real failure and is thrown on. */
async function addressOrNone(): Promise<string | null> {
  try {
    return (await intake.address()).address;
  } catch (error: unknown) {
    if (error instanceof ApiError && error.status === 503) return null;
    throw error;
  }
}

export function useForwarding(): ForwardingState {
  const [attempt, setAttempt] = useState(0);
  const [good, setGood] = useState<Good | null>(null);
  const [failed, setFailed] = useState<Failed | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([addressOrNone(), intake.emails()])
      .then(([address, emails]) => {
        if (!cancelled) setGood({ attempt, data: { address, emails } });
      })
      .catch((error: unknown) => {
        if (!cancelled) setFailed({ attempt, message: describeError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const again = useCallback(() => setAttempt((n) => n + 1), []);
  const failedNow = failed !== null && failed.attempt === attempt ? failed.message : null;

  if (good === null) {
    return failedNow !== null
      ? { status: "error", message: failedNow, retry: again }
      : { status: "loading" };
  }
  return { status: "ready", data: good.data, reload: again, reloadError: failedNow };
}
