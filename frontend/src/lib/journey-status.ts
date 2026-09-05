/**
 * One line of status per journey, worked out from the journey and its claim.
 *
 * The backend keeps two state machines: a journey is pending → matched →
 * assessed (or unmatched), and a claim runs draft → ready → submitted →
 * paid, with needs_user, rejected and expired as side exits. A person needs
 * neither. They need one label, one colour, an amount if there is one, and
 * to know whether there is something to press. This function is that
 * translation, kept pure so it can be tested as a table.
 *
 * The File button follows the API's own rule (claims.service.file_claim):
 * only a draft or needs_user claim whose deadline has not passed can be
 * filed by the user. `ready` means the system is about to file it, and a
 * claim past file_by is refused, so neither gets the button.
 */

import type { Claim, Journey } from "@/lib/api";
import { londonDate } from "@/lib/format";

export type Tone = "muted" | "brand" | "cta" | "good" | "bad";

export type JourneyView = {
  label: string;
  tone: Tone;
  /** The claim's amount, when there is a claim. */
  amountPence: number | null;
  /** The claim's filing deadline, when it still matters. */
  fileBy: string | null;
  /** Whether the File button applies: the API would accept a filing now. */
  canFile: boolean;
};

export function describeJourney(journey: Journey, claim: Claim | undefined, now: Date): JourneyView {
  if (claim !== undefined) return describeClaim(claim, now);

  switch (journey.status) {
    case "assessed":
      // Assessed with no claim. Usually on time or under the operator's
      // threshold — but also, for up to one scheduler interval, a delay
      // whose claim has not been opened yet, and permanently a delay on an
      // operator the backend has no scheme for. "No claim" is true in every
      // case; anything stronger is a guess.
      return view("No claim", "muted");
    case "unmatched":
      return view("Couldn't find this train", "bad");
    default:
      // pending or matched: being watched. Before the scheduled arrival it
      // is simply in the future; after it, the next sweep will judge it.
      return new Date(journey.scheduled_arrival) > now
        ? view("Upcoming", "muted")
        : view("Checking", "brand");
  }
}

function describeClaim(claim: Claim, now: Date): JourneyView {
  const amount = claim.amount_pence;
  // Deadlines are UK dates; compare with today's London date, as the API does.
  const overdue = claim.file_by < londonDate(now);

  switch (claim.status) {
    case "draft":
      return overdue
        ? { label: "Deadline passed", tone: "bad", amountPence: amount, fileBy: null, canFile: false }
        : { label: "Ready to file", tone: "cta", amountPence: amount, fileBy: claim.file_by, canFile: true };
    case "needs_user":
      // The person was handed the operator's page. The backend cannot see
      // whether they finished, so past the deadline it can only ask.
      return overdue
        ? { label: "Deadline passed — did you file it?", tone: "bad", amountPence: amount, fileBy: null, canFile: false }
        : { label: "File it with the operator", tone: "cta", amountPence: amount, fileBy: claim.file_by, canFile: true };
    case "ready":
      // Queued for the system to file on the user's behalf (v2). Not theirs
      // to press: the API refuses a user filing in this state.
      return { label: "Being filed for you", tone: "brand", amountPence: amount, fileBy: claim.file_by, canFile: false };
    case "submitted":
    case "approved":
      return { label: "Claim sent", tone: "brand", amountPence: amount, fileBy: null, canFile: false };
    case "paid":
      return { label: "Paid", tone: "good", amountPence: amount, fileBy: null, canFile: false };
    case "rejected":
      return { label: "Rejected by the operator", tone: "bad", amountPence: amount, fileBy: null, canFile: false };
    case "expired":
      return { label: "Deadline passed", tone: "bad", amountPence: amount, fileBy: null, canFile: false };
    default:
      // A status this build does not know. Show it rather than hide it.
      return { label: claim.status, tone: "muted", amountPence: amount, fileBy: null, canFile: false };
  }
}

function view(label: string, tone: Tone): JourneyView {
  return { label, tone, amountPence: null, fileBy: null, canFile: false };
}
