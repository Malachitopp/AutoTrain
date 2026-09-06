/**
 * What one forwarded email's status means to the person who sent it.
 *
 * The API's statuses are the backend's words (migration 0013, and
 * 'confirmation' from 0016). This turns each into a short label, a colour,
 * and whether the row is something the user has to act on. The API's own
 * status_reason is shown underneath as the detail, because it is written
 * for a person already: "2 journey(s) added", "reader unsure: price
 * unclear", "daily limit of 50 emails reached".
 *
 * The counterpart of lib/journey-status.ts: one place decides how a backend
 * status reads, so no component invents its own wording.
 */

import type { InboundEmail } from "@/lib/api";
import type { Tone } from "@/lib/journey-status";

export type InboundView = {
  label: string;
  tone: Tone;
  /** True for the one row a person must do something about: the code their
   * email provider sent to switch automatic forwarding on. */
  needsAction: boolean;
};

const VIEWS: Record<string, { label: string; tone: Tone }> = {
  received: { label: "Reading it", tone: "muted" },
  parsed: { label: "Journey added", tone: "good" },
  needs_review: { label: "Needs a look", tone: "cta" },
  duplicate: { label: "Already tracked", tone: "muted" },
  rejected: { label: "Not a ticket", tone: "muted" },
  failed: { label: "Could not read it", tone: "bad" },
  confirmation: { label: "Action needed", tone: "cta" },
};

export function describeInbound(email: InboundEmail): InboundView {
  // An unknown status means the backend grew one and this file has not
  // caught up. Show it rather than hide the row.
  const view = VIEWS[email.status] ?? { label: email.status, tone: "muted" as Tone };
  return { ...view, needsAction: email.status === "confirmation" };
}

/** The setup code a mail provider sent, if one is waiting. Only the newest
 * matters: an older code has been superseded or has expired. */
export function pendingSetup(emails: InboundEmail[]): InboundEmail | null {
  return emails.find((email) => email.status === "confirmation") ?? null;
}
