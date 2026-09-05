/**
 * One line of status per journey, worked out from the journey and its claim.
 *
 * The backend keeps two state machines: a journey is pending → matched →
 * assessed (or unmatched), and a claim runs draft → ready → submitted →
 * paid, with needs_user, rejected and expired as side exits. A person needs
 * neither. They need one label, one colour, an amount if there is one, and
 * to know whether there is something to press. This function is that
 * translation, kept pure so it can be tested as a table.
 */

import type { Claim, Journey } from "@/lib/api";

export type Tone = "muted" | "brand" | "cta" | "good" | "bad";

export type JourneyView = {
  label: string;
  tone: Tone;
  /** The claim's amount, when there is a claim. */
  amountPence: number | null;
  /** The claim's filing deadline, when filing is still the user's move. */
  fileBy: string | null;
  /** Whether the File button applies: the claim is waiting on the user. */
  canFile: boolean;
};

export function describeJourney(journey: Journey, claim: Claim | undefined, now: Date): JourneyView {
  if (claim !== undefined) return describeClaim(claim);

  switch (journey.status) {
    case "assessed":
      // Assessed with no claim: on time, or late but under the operator's
      // threshold. Either way nothing is owed.
      return view("No refund due", "muted");
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

function describeClaim(claim: Claim): JourneyView {
  const amount = claim.amount_pence;
  switch (claim.status) {
    case "draft":
    case "ready":
      return { label: "Ready to file", tone: "cta", amountPence: amount, fileBy: claim.file_by, canFile: true };
    case "needs_user":
      return { label: "File it with the operator", tone: "cta", amountPence: amount, fileBy: claim.file_by, canFile: true };
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
